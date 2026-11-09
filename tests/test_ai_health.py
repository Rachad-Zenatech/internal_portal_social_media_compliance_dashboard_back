import unittest
from datetime import datetime, timedelta, timezone

from services.ai.observability import ai_health
from services.ai.observability.ai_observability import classify_error

NOW = datetime(2026, 11, 5, 15, 0, tzinfo=timezone.utc)


def call(minutes_ago, status="OK", kind=None):
    return {"status": status, "kind": kind, "started_at": NOW - timedelta(minutes=minutes_ago)}


class FakeError(Exception):
    def __init__(self, message, code=None):
        super().__init__(message)
        self.code = code


class AiStatusTests(unittest.TestCase):
    def test_no_calls_yet_is_fine(self):
        self.assertTrue(ai_health.summarize([], NOW).available)

    def test_the_latest_call_working_is_fine(self):
        self.assertTrue(ai_health.summarize([call(1), call(2, "ERROR", "QUOTA")], NOW).available)

    def test_one_model_failing_just_after_another_worked_is_the_fallback_not_an_outage(self):
        self.assertTrue(ai_health.summarize([call(0.2, "ERROR", "QUOTA"), call(0.5)], NOW).available)

    def test_everything_failing_for_a_while_is_down_and_says_why(self):
        rows = [call(1, "ERROR", "KEY"), call(2, "ERROR", "KEY"), call(20)]
        status = ai_health.summarize(rows, NOW)
        self.assertFalse(status.available)
        self.assertEqual(status.reason, "KEY")
        self.assertIn("renew", status.message)

    def test_a_key_problem_outranks_a_quota_one(self):
        rows = [call(3, "ERROR", "QUOTA"), call(4, "ERROR", "KEY")]
        self.assertEqual(ai_health.summarize(rows, NOW).reason, "KEY")

    def test_a_used_up_quota_says_so(self):
        status = ai_health.summarize([call(3, "ERROR", "QUOTA"), call(4, "ERROR", "MODEL")], NOW)
        self.assertEqual((status.available, status.reason), (False, "QUOTA"))
        self.assertIn("limit", status.title)

    def test_no_key_configured_is_down_without_looking_at_calls(self):
        status = ai_health.summarize([], NOW, key_set=False)
        self.assertEqual((status.available, status.reason), (False, "KEY"))

    def test_it_comes_back_as_soon_as_a_call_works(self):
        self.assertTrue(ai_health.summarize([call(0.1), call(5, "ERROR", "KEY")], NOW).available)


class ClassifyTests(unittest.TestCase):
    def test_kinds(self):
        self.assertEqual(classify_error(FakeError("API key expired. Please renew the API key.", 400)), "KEY")
        self.assertEqual(classify_error(FakeError("permission denied", 403)), "KEY")
        self.assertEqual(classify_error(FakeError("429 RESOURCE_EXHAUSTED. Quota exceeded", 429)), "QUOTA")
        self.assertEqual(classify_error(FakeError("models/x is no longer available", 404)), "MODEL")
        self.assertEqual(classify_error(FakeError("503 UNAVAILABLE", 503)), "BUSY")
        self.assertEqual(classify_error(TimeoutError("read timed out")), "BUSY")
        self.assertEqual(classify_error(ValueError("odd")), "OTHER")



class CheckWithoutTheAiTests(unittest.IsolatedAsyncioTestCase):
    """With no working AI the check must say so, not hand back a clean 100."""

    async def run_check(self, env, **patches):
        import os
        from unittest.mock import AsyncMock, patch

        from models.compliance.compliance_model import ComplianceCheckRequest
        from services.compliance.evaluation import compliance_service

        with (
            patch.dict(os.environ, env, clear=False),
            patch.object(compliance_service, "applicable_rules", new=AsyncMock(return_value=[])),
            patch.object(compliance_service, "rulebook_version", new=AsyncMock(return_value=None)),
        ):
            return await compliance_service.check_compliance(ComplianceCheckRequest(text="Reclaim up to 3,000 hours of work."))

    async def test_no_key_marks_the_check_as_not_fully_read(self):
        result = await self.run_check({"GEMINI_API_KEY": ""})
        self.assertTrue(result.ai_unavailable)

    async def test_the_meaning_check_switched_off_on_purpose_is_not_a_failure(self):
        result = await self.run_check({"GEMINI_API_KEY": "", "COMPLIANCE_AI_VIABILITY_ENABLED": "false"})
        self.assertFalse(result.ai_unavailable)


if __name__ == "__main__":
    unittest.main()
