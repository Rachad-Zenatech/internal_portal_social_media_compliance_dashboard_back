import unittest
from datetime import datetime, timedelta, timezone

from fastapi import HTTPException

from services.compliance.submissions import review_deadline_service as rds
from services.compliance.submissions.submission_service import _due


def hours(n):
    return datetime.now(timezone.utc) + timedelta(hours=n)


class DeadlineTests(unittest.TestCase):
    def test_a_date_the_reviewer_picked_is_kept_even_past_the_publish_time(self):
        at = hours(24 * 10)
        due = rds.revision_due(publish_at=hours(24 * 3), days=None, at=at)
        self.assertEqual(due, at)

    def test_working_days_are_still_pulled_in_by_a_close_publish_time(self):
        publish = hours(12)
        due = rds.revision_due(publish_at=publish, days=5, at=None)
        self.assertLessEqual(due, publish - timedelta(hours=rds.REVISION_BEFORE_PUBLISH_HOURS) + timedelta(hours=rds.MIN_HOURS))

    def test_a_date_in_the_past_is_refused(self):
        with self.assertRaises(HTTPException) as err:
            rds.revision_due(publish_at=None, days=None, at=hours(-2))
        self.assertEqual(err.exception.status_code, 422)
        with self.assertRaises(HTTPException):
            rds.video_due(publish_at=None, days=None, at=hours(-2))

    def test_the_video_has_no_deadline_unless_one_is_given(self):
        self.assertIsNone(rds.video_due(None, None, None))
        self.assertIsNotNone(rds.video_due(None, 3, None))
        at = hours(72)
        self.assertEqual(rds.video_due(None, None, at), at)

    def test_a_script_deadline_shows_only_at_its_step(self):
        due = hours(10)
        base = {"due_at": due, "status": "DRAFT"}
        self.assertEqual(_due({**base, "due_kind": "VIDEO", "script_status": "APPROVED"})["due_kind"], "VIDEO")
        self.assertIsNone(_due({**base, "due_kind": "VIDEO", "script_status": "IN_REVIEW"})["due_at"])
        self.assertEqual(_due({**base, "due_kind": "REVISION", "script_status": "EDITS_PROPOSED"})["due_kind"], "REVISION")
        self.assertIsNone(_due({**base, "due_kind": "REVISION", "script_status": "APPROVED"})["due_at"])
        # Posts keep working as before.
        self.assertEqual(_due({"due_at": due, "status": "CHANGES_REQUESTED", "due_kind": "REVISION"})["due_kind"], "REVISION")

    def test_the_video_notices_read_naturally(self):
        row = {"reference_code": "SUB-1", "title": "Test", "due_kind": "VIDEO", "due_at": hours(2)}
        self.assertIn("Upload the video by", rds._message(row, "soon")["message"])
        self.assertIn("overdue", rds._message(row, "overdue")["title"])


if __name__ == "__main__":
    unittest.main()
