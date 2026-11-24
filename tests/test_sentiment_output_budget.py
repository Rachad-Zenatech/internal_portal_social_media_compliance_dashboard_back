"""Sentiment truncation is retried with headroom, never presented as an empty valid assessment."""
import json
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from services.ai.core.bedrock_service import BedrockClientAdapter, BedrockResponse
from services.compliance.sentiment import engine
from services.compliance.sentiment.response_format import OutputLimitReached, parse_response

VALID = {
    "summary": "Informative post.", "overall_assessment": "NO_CONCERNS", "reputational_concern": "NONE",
    "tone": {"primary": "informative", "intensity": "LOW", "evidence": []},
    "brand_alignment": {"rating": "ALIGNED", "explanation": "Matches the supplied guidance."},
    "controversy": {"applies": False}, "findings": [],
}


class SentimentResponseTests(unittest.TestCase):
    def test_actual_failure_shape_is_recognized_as_output_exhaustion(self):
        response = BedrockResponse("", {"outputTokens": 4096})
        with self.assertRaises(OutputLimitReached):
            parse_response(response, 4096)

    def test_provider_stop_reason_rejects_partial_assessment(self):
        with self.assertRaises(OutputLimitReached):
            parse_response(BedrockResponse('{"summary":"partial"}', stop_reason="max_tokens"), 16384)

    def test_empty_response_is_not_converted_to_an_empty_json_assessment(self):
        with self.assertRaisesRegex(ValueError, "no assessment text"):
            parse_response(BedrockResponse(""), 16384)

    def test_complete_assessment_is_validated(self):
        self.assertEqual(parse_response(BedrockResponse(json.dumps(VALID)), 16384).summary, VALID["summary"])
        with self.assertRaises(ValueError):
            parse_response(BedrockResponse('{}'), 16384)

    def test_bedrock_preserves_stop_reason_and_configured_budget(self):
        adapter = BedrockClientAdapter()
        adapter._boto_client = MagicMock()
        adapter._boto_client.converse.return_value = {
            "stopReason": "max_tokens", "usage": {"outputTokens": 16384},
            "output": {"message": {"content": [{"reasoningContent": {}}]}},
        }
        result = adapter.models.generate_content(model="test-model", contents="test",
                                                  config=SimpleNamespace(max_output_tokens=16384))
        self.assertEqual(result.text, "")
        self.assertEqual(result.stop_reason, "max_tokens")
        self.assertEqual(adapter._boto_client.converse.call_args.kwargs["inferenceConfig"]["maxTokens"], 16384)


class SentimentBudgetTests(unittest.IsolatedAsyncioTestCase):
    async def test_retry_increases_budget_and_returns_valid_assessment(self):
        budgets = []
        async def generate(client, **kwargs):
            budgets.append(kwargs["config"].max_output_tokens)
            if len(budgets) == 1:
                return BedrockResponse("", {"outputTokens": 16384}, stop_reason="max_tokens")
            return BedrockResponse(json.dumps(VALID), stop_reason="end_turn")

        with patch.object(engine, "MAX_OUTPUT_TOKENS", 16384), \
             patch.object(engine, "_prompt", return_value="Assess this synthetic post."), \
             patch.object(engine.ai_observability, "generate", AsyncMock(side_effect=generate)), \
             patch.object(engine, "_verify", side_effect=lambda result, *args, **kwargs: result):
            result = await engine._generate_with(object(), "test-model", {"title": "test"}, None)
        self.assertEqual(budgets, [16384, 32768])
        self.assertEqual(result.summary, VALID["summary"])

    async def test_persistent_truncation_has_a_clear_error_and_bounded_retries(self):
        generate = AsyncMock(return_value=BedrockResponse("", stop_reason="max_tokens"))
        with patch.object(engine, "MAX_OUTPUT_TOKENS", 16384), \
             patch.object(engine, "_prompt", return_value="Synthetic post"), \
             patch.object(engine.ai_observability, "generate", generate):
            with self.assertRaisesRegex(RuntimeError, "exceeded the response limit"):
                await engine._generate_with(object(), "test-model", {}, None)
        self.assertEqual(generate.await_count, 2)

    async def test_other_malformed_output_does_not_expose_pydantic_stack_to_user(self):
        generate = AsyncMock(return_value=BedrockResponse('{}'))
        with patch.object(engine, "_prompt", return_value="Synthetic post"), \
             patch.object(engine.ai_observability, "generate", generate):
            with self.assertRaisesRegex(RuntimeError, "incomplete sentiment assessment") as error:
                await engine._generate_with(object(), "test-model", {}, None)
        self.assertNotIn("pydantic", str(error.exception).lower())
        self.assertEqual(generate.await_count, 2)
