"""Assessment validation happens before a model call is marked successful."""
import unittest
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from services.ai.observability import ai_observability as obs
from services.ai.core.bedrock_service import BedrockResponse
from services.ai.core.response_errors import InvalidResponseError, OutputLimitError


class ResponseDiagnosticsTests(unittest.IsolatedAsyncioTestCase):
    async def call(self, validator, response=None, error=None):
        state = SimpleNamespace(attributes={})
        @asynccontextmanager
        async def span(*args, **kwargs):
            yield state
        client = MagicMock()
        client.models.generate_content.return_value = response or BedrockResponse('{}', {"outputTokens": 25}, stop_reason="end_turn")
        client.models.generate_content.side_effect = error
        with patch.object(obs, "span", span), patch.object(obs, "log_prompt") as logged:
            try:
                await obs.generate(client, model="test-model", contents="synthetic", feature="SENTIMENT", validate_response=validator)
            except (InvalidResponseError, OutputLimitError):
                pass
        return state.attributes, logged.call_args.kwargs

    async def test_schema_failure_is_not_logged_as_success(self):
        def reject(response):
            raise InvalidResponseError("Missing required assessment field")
        attributes, logged = await self.call(reject)
        self.assertEqual(attributes["error_kind"], "RESPONSE_INVALID")
        self.assertFalse(attributes["assessment_validated"])
        self.assertEqual(attributes["output_tokens"], 25)
        self.assertEqual(logged["status"], "ERROR")

    async def test_output_limit_retains_usage_and_stop_reason(self):
        error = OutputLimitError(token_limit=8192, response=BedrockResponse('', {"outputTokens": 8192}, stop_reason="max_tokens"))
        attributes, logged = await self.call(lambda r: None, error=error)
        self.assertEqual(attributes["error_kind"], "OUTPUT_LIMIT")
        self.assertEqual(attributes["stop_reason"], "max_tokens")
        self.assertEqual(logged["output_tokens"], 8192)

    async def test_completed_assessment_records_validation(self):
        attributes, logged = await self.call(lambda r: None)
        self.assertTrue(attributes["assessment_validated"])
        self.assertEqual(attributes["stop_reason"], "end_turn")
        self.assertNotEqual(logged.get("status"), "ERROR")
