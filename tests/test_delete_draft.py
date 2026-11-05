import unittest
from datetime import datetime, timezone

from services.compliance import submission_service as subs


class WhoCanBeDeletedTests(unittest.TestCase):
    def test_a_draft_can_be_deleted_by_whoever_may(self):
        self.assertTrue(subs._can_delete({"status": "DRAFT"}, True))
        self.assertFalse(subs._can_delete({"status": "DRAFT"}, False))

    def test_content_in_review_or_approved_cannot(self):
        for status in ("IN_REVIEW", "APPROVED", "CHANGES_REQUESTED"):
            self.assertFalse(subs._can_delete({"status": status}, True), status)

    def test_a_draft_whose_script_is_with_a_reviewer_cannot(self):
        self.assertFalse(subs._can_delete({"status": "DRAFT", "script_status": "IN_REVIEW"}, True))

    def test_a_draft_whose_script_is_being_sent_cannot(self):
        self.assertFalse(subs._can_delete({"status": "DRAFT", "script_send_status": "PROCESSING"}, True))
        now = datetime.now(timezone.utc).isoformat()
        raw = {"status": "PROCESSING", "started_at": now}
        self.assertFalse(subs._can_delete({"status": "DRAFT", "script_send": raw}, True))

    def test_a_script_that_was_sent_back_or_never_sent_does_not_stop_it(self):
        for script in (None, "DRAFT", "CHANGES_REQUESTED", "EDITS_PROPOSED", "APPROVED"):
            self.assertTrue(subs._can_delete({"status": "DRAFT", "script_status": script}, True), script)

    def test_a_stuck_send_does_not_stop_it_forever(self):
        old = {"status": "PROCESSING", "started_at": "2020-01-01T00:00:00+00:00"}
        self.assertTrue(subs._can_delete({"status": "DRAFT", "script_send": old}, True))


if __name__ == "__main__":
    unittest.main()
