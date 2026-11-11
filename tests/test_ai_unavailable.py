import unittest
from datetime import datetime, timezone
from uuid import uuid4

from services.compliance.submissions import submission_service
from services.compliance.submissions.submission_service import ai_unavailable


def job(check, score=100):
    return {
        "id": uuid4(), "version_number": 1, "status": "AWAITING_REVIEW", "reviewer_route": [],
        "traffic_light": "YELLOW", "score": score, "automated_check": check,
        "created_at": datetime.now(timezone.utc), "completed_at": None,
    }


class AiUnavailableTests(unittest.TestCase):
    def test_flag_comes_from_the_stored_check(self):
        self.assertTrue(ai_unavailable({"ai_unavailable": True}))
        self.assertFalse(ai_unavailable({"ai_unavailable": False}))
        self.assertFalse(ai_unavailable({}))
        self.assertFalse(ai_unavailable(None))

    def test_a_review_job_has_no_score_while_the_ai_is_unavailable(self):
        check = {"analysis_pending": False, "ai_unavailable": True}
        self.assertIsNone(submission_service._review_job(job(check)).score)
        self.assertEqual(submission_service._review_job(job({"analysis_pending": False})).score, 100)

    def test_messages_say_there_is_no_score(self):
        class Result:
            score = 100
            violations_count = 0

        check = {"analysis_pending": False, "ai_unavailable": True}
        self.assertIn("no score", submission_service._score_line(Result(), check))
        self.assertIn("no score", submission_service._check_summary(Result(), check))
        self.assertNotIn("100/100", submission_service._score_line(Result(), check))

    def test_listing_sql_hides_the_score(self):
        self.assertIn("ai_unavailable", submission_service._unavailable_sql("s.latest_check"))


if __name__ == "__main__":
    unittest.main()
