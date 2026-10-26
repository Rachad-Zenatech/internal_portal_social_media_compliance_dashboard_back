import unittest
from datetime import datetime, timezone
from uuid import uuid4

from services import submission_service
from services.submission_service import analysis_pending


def job(check, score=100):
    return {
        "id": uuid4(),
        "version_number": 1,
        "status": "AWAITING_REVIEW",
        "reviewer_route": [],
        "traffic_light": "GREEN",
        "score": score,
        "automated_check": check,
        "created_at": datetime.now(timezone.utc),
        "completed_at": None,
    }


class AnalysisPendingTests(unittest.TestCase):
    def test_pending_while_the_transcript_or_image_text_is_being_read(self):
        self.assertTrue(analysis_pending({"transcript_pending": True, "images_pending": 0}))
        self.assertTrue(analysis_pending({"transcript_pending": False, "images_pending": 1}))

    def test_done_once_everything_is_read_or_could_not_be_read(self):
        self.assertFalse(analysis_pending({"transcript_pending": False, "images_pending": 0, "images_failed": 1}))
        self.assertFalse(analysis_pending(None))

    def test_the_stored_flag_wins(self):
        self.assertFalse(analysis_pending({"analysis_pending": False, "transcript_pending": True}))

    def test_a_review_job_has_no_score_until_the_post_is_read(self):
        self.assertIsNone(submission_service._review_job(job({"transcript_pending": True})).score)
        self.assertEqual(submission_service._review_job(job({"transcript_pending": False, "images_pending": 0})).score, 100)

    def test_messages_say_under_review_instead_of_a_score(self):
        class Result:
            score = 100
            violations_count = 0

        self.assertIn("under review", submission_service._score_line(Result(), {"images_pending": 2}))
        self.assertIn("100/100", submission_service._score_line(Result(), {"images_pending": 0}))


if __name__ == "__main__":
    unittest.main()
