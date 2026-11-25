import json
import unittest

from botocore.exceptions import ClientError

from services.ai.core.ai_error_diagnostics import classify_error, user_message
from services.shared.core.logging_service import request_id_ctx


class AIErrorDiagnosticsTests(unittest.TestCase):
    def test_provider_failures_and_secrets(self):
        for code, expected in [("AccessDeniedException", "access_denied"),
                               ("ThrottlingException", "provider_quota"),
                               ("ValidationException", "model_configuration")]:
            error = ClientError({"Error": {"Code": code, "Message": "secret-token"}}, "Converse")
            self.assertEqual(classify_error(error)[0], expected)
            self.assertNotIn("secret-token", user_message(error))

    def test_nested_failure_and_output_limit(self):
        try:
            try:
                raise ValueError("Bedrock JSON response exceeded its output token limit")
            except ValueError as exc:
                raise RuntimeError("fallback failed") from exc
        except RuntimeError as exc:
            self.assertEqual(classify_error(exc)[0], "output_limit")

    def test_timeout_invalid_json_and_unknown(self):
        self.assertEqual(classify_error(TimeoutError())[0], "timeout")
        self.assertEqual(classify_error(json.JSONDecodeError("bad", "x", 0))[0], "invalid_response")
        self.assertNotIn("secret-token", user_message(RuntimeError("secret-token")))

    def test_reference(self):
        token = request_id_ctx.set("request-123")
        try:
            self.assertIn("request-123", user_message(TimeoutError()))
        finally:
            request_id_ctx.reset(token)
