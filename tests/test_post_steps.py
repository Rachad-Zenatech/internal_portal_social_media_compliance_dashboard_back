"""The five steps a creator works through: which are done, which are out of date, where to carry on, and when a post can be sent."""

import unittest
from datetime import datetime, timedelta, timezone

from services.compliance.submissions import post_steps_service as ps

NOW = datetime(2026, 11, 10, 15, 0, tzinfo=timezone.utc)


def post(**over):
    """A draft with everything filled in and every tool run after the last edit."""
    row = {
        "status": "DRAFT", "content_type": "IMAGE", "title": "Solar survey win",
        "brand_codes": ["ZENATECH"], "audiences": ["INVESTORS"], "region": "Global", "platforms": ["LINKEDIN"],
        "has_account": True, "rule_sets": ["ZenaTech General"], "caption_text": "A caption.",
        "main_required": True, "has_main": True, "main_is_video": False, "script_stage": False, "script_status": None,
        "created_at": NOW - timedelta(days=2), "content_edited_at": NOW - timedelta(hours=3),
        "check_at": NOW - timedelta(hours=1), "latest_score": 80, "analysis_pending": False, "ai_unavailable": False,
        "sentiment_at": NOW - timedelta(hours=1), "sentiment_running": False, "mood_at": NOW - timedelta(hours=1),
    }
    row.update(over)
    return row


class StepsTests(unittest.TestCase):
    def test_a_finished_draft_is_ready_to_send_and_sits_on_the_send_step(self):
        s = ps.compute_steps(post())
        self.assertEqual((s.details, s.compliance, s.sentiment, s.mood, s.send), ("done", "done", "done", "done", "todo"))
        self.assertEqual((s.next, s.ready_to_send, s.locked), ("send", True, False))

    def test_a_new_draft_with_nothing_filled_in_starts_at_details(self):
        s = ps.compute_steps(post(title="", caption_text="", has_main=False, check_at=None, latest_score=None,
                                  sentiment_at=None, mood_at=None))
        self.assertEqual((s.details, s.compliance, s.sentiment, s.mood), ("todo", "todo", "todo", "todo"))
        self.assertEqual((s.next, s.ready_to_send), ("details", False))
        self.assertEqual(set(s.missing), {"title", "caption", "main_file"})

    def test_it_carries_on_at_the_first_step_not_done(self):
        s = ps.compute_steps(post(sentiment_at=None, mood_at=None))
        self.assertEqual((s.next, s.ready_to_send), ("sentiment", False))
        s = ps.compute_steps(post(mood_at=None))
        self.assertEqual((s.next, s.ready_to_send), ("mood", False))

    def test_a_tool_run_before_the_last_edit_is_out_of_date(self):
        edited = NOW - timedelta(minutes=5)
        s = ps.compute_steps(post(content_edited_at=edited))
        self.assertEqual((s.compliance, s.sentiment, s.mood), ("stale", "stale", "stale"))
        self.assertEqual((s.next, s.ready_to_send), ("compliance", False))

    def test_an_edit_before_the_tools_ran_leaves_them_current(self):
        s = ps.compute_steps(post(content_edited_at=NOW - timedelta(hours=5)))
        self.assertEqual((s.compliance, s.sentiment, s.mood), ("done", "done", "done"))

    def test_a_post_never_edited_counts_from_when_it_was_created(self):
        s = ps.compute_steps(post(content_edited_at=None, created_at=NOW - timedelta(days=2)))
        self.assertEqual(s.compliance, "done")

    def test_a_check_still_being_read_is_running_and_not_ready(self):
        s = ps.compute_steps(post(analysis_pending=True, latest_score=None))
        self.assertEqual((s.compliance, s.ready_to_send), ("running", False))

    def test_a_sentiment_run_in_progress_is_running(self):
        s = ps.compute_steps(post(sentiment_running=True))
        self.assertEqual((s.sentiment, s.ready_to_send), ("running", False))

    def test_a_check_the_ai_could_not_finish_has_to_be_run_again(self):
        s = ps.compute_steps(post(ai_unavailable=True, latest_score=None))
        self.assertEqual((s.compliance, s.ready_to_send), ("todo", False))

    def test_a_check_saved_before_checks_were_timed_counts_as_current(self):
        s = ps.compute_steps(post(check_at=None, latest_score=85))
        self.assertEqual(s.compliance, "done")

    def test_incomplete_details_stop_it_being_ready_even_if_every_tool_ran(self):
        s = ps.compute_steps(post(platforms=[]))
        self.assertEqual((s.details, s.ready_to_send, s.next), ("todo", False, "details"))
        self.assertIn("platforms", s.missing)

    def test_a_text_post_needs_no_media(self):
        s = ps.compute_steps(post(content_type="TEXT", main_required=False, has_main=False))
        self.assertEqual(s.details, "done")

    def test_a_social_post_with_a_video_is_missing_its_media(self):
        s = ps.compute_steps(post(content_type="SOCIAL_POST", main_required=False, main_is_video=True))
        self.assertIn("main_file", s.missing)

    def test_a_video_needs_its_script_approved_first(self):
        s = ps.compute_steps(post(script_stage=True, script_status="DRAFT"))
        self.assertIn("script_approval", s.missing)
        s = ps.compute_steps(post(script_stage=True, script_status="APPROVED"))
        self.assertEqual(s.details, "done")

    def test_the_fields_that_belong_to_sending_are_not_part_of_the_details_step(self):
        s = ps.compute_steps(post(reviewer_ids=[], approval_needed_by=None))
        self.assertEqual(s.details, "done")


class LockedTests(unittest.TestCase):
    def test_a_sent_post_is_locked_and_everything_is_done(self):
        for status in ("IN_REVIEW", "APPROVED", "REJECTED"):
            with self.subTest(status=status):
                s = ps.compute_steps(post(status=status, check_at=None, latest_score=None, sentiment_at=None, mood_at=None))
                self.assertTrue(s.locked)
                self.assertEqual((s.details, s.compliance, s.sentiment, s.mood, s.send), ("done",) * 5)
                self.assertEqual((s.next, s.ready_to_send), (None, False))

    def test_changes_requested_unlocks_it_and_the_steps_are_worked_out_again(self):
        s = ps.compute_steps(post(status="CHANGES_REQUESTED", content_edited_at=NOW))
        self.assertFalse(s.locked)
        self.assertEqual((s.compliance, s.send, s.next), ("stale", "todo", "compliance"))

    def test_changes_requested_with_nothing_edited_can_be_resent(self):
        s = ps.compute_steps(post(status="CHANGES_REQUESTED"))
        self.assertEqual((s.locked, s.ready_to_send, s.next), (False, True, "send"))


if __name__ == "__main__":
    unittest.main()
