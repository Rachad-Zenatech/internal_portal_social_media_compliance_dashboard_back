"""Bedrock structured responses must be complete before compliance uses them."""
import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock

from services.ai.core.bedrock_service import BedrockClientAdapter


def response(text, reason="end_turn"):
    return {"stopReason": reason, "output": {"message": {"content": [{"text": text}]}}}


class BedrockJsonRetryTests(unittest.TestCase):
    def setUp(self):
        self.adapter = BedrockClientAdapter()
        self.adapter._boto_client = MagicMock()
        self.config = SimpleNamespace(response_mime_type="application/json", max_output_tokens=1200)

    def test_truncated_json_retries_with_more_output_space(self):
        client = self.adapter._boto_client
        client.converse.side_effect = [response('{"value":', "max_tokens"), response('{"value": 1}')]
        result = self.adapter.models.generate_content(model="test-model", contents="test", config=self.config)
        self.assertEqual(result.parsed, {"value": 1})
        self.assertEqual(client.converse.call_count, 2)
        self.assertEqual(client.converse.call_args.kwargs["inferenceConfig"]["maxTokens"], 4096)

    def test_malformed_json_retries_even_without_truncation_signal(self):
        client = self.adapter._boto_client
        client.converse.side_effect = [response('{"value":'), response('{"value": 1}')]
        self.adapter.models.generate_content(model="test-model", contents="test", config=self.config)
        self.assertEqual(client.converse.call_count, 2)

    def test_persistent_truncation_cannot_be_accepted_as_valid_json(self):
        self.adapter._boto_client.converse.return_value = response('{}', "max_tokens")
        with self.assertRaisesRegex(ValueError, "output token limit"):
            self.adapter.models.generate_content(model="test-model", contents="test", config=self.config)
        self.assertEqual(self.adapter._boto_client.converse.call_count, 2)

    def test_valid_json_is_not_retried(self):
        self.adapter._boto_client.converse.return_value = response('{}')
        self.adapter.models.generate_content(model="test-model", contents="test", config=self.config)
        self.adapter._boto_client.converse.assert_called_once()

    def test_caller_can_handle_truncation_without_adapter_retry(self):
        self.config.retry_invalid_json = False
        self.adapter._boto_client.converse.return_value = response('{"value":', "max_tokens")
        result = self.adapter.models.generate_content(model="test-model", contents="test", config=self.config)
        self.assertEqual(result.stop_reason, "max_tokens")
        self.adapter._boto_client.converse.assert_called_once()
