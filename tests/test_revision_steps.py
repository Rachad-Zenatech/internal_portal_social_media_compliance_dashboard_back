import unittest
from datetime import timedelta
from unittest.mock import AsyncMock, patch
from uuid import uuid4
from tests.test_post_steps import NOW, post
from services.compliance.submissions import post_steps_service as ps, revision_steps


class RevisionTests(unittest.TestCase):
    def revision(self, **over):
        return ps.compute_steps(post(status="CHANGES_REQUESTED", revision_at=NOW,
                                     unresolved_comments=False, **over))

    def test_requesting_changes_resets_old_details_checks_and_send(self):
        steps = self.revision()
        self.assertTrue(steps.revision)
        for key in ("suggestions", "details", "compliance", "sentiment", "send"):
            self.assertEqual(getattr(steps, key), "todo")
        self.assertEqual(steps.next, "suggestions")
        self.assertFalse(steps.ready_to_send)

    def test_revisions_only_count_checks_after_return_and_latest_edit(self):
        steps = self.revision(content_edited_at=NOW + timedelta(minutes=1),
                              check_at=NOW + timedelta(minutes=2), sentiment_at=NOW - timedelta(minutes=1))
        self.assertEqual((steps.suggestions, steps.details, steps.compliance, steps.sentiment),
                         ("done", "done", "done", "todo"))
        self.assertEqual(steps.next, "sentiment")

    def test_checks_become_stale_if_creator_edits_again(self):
        steps = self.revision(content_edited_at=NOW + timedelta(minutes=3), check_at=NOW + timedelta(minutes=2))
        self.assertEqual(steps.compliance, "stale")
        self.assertEqual(steps.next, "compliance")

    def test_all_fresh_required_steps_unlock_reviewers(self):
        steps = self.revision(content_edited_at=NOW + timedelta(minutes=1),
                              check_at=NOW + timedelta(minutes=2), sentiment_at=NOW + timedelta(minutes=2))
        self.assertTrue(steps.ready_to_send)
        self.assertEqual(steps.next, "send")
        self.assertEqual(steps.send, "todo")

    def test_another_change_request_starts_another_round(self):
        row = post(status="CHANGES_REQUESTED", unresolved_comments=False,
                   content_edited_at=NOW + timedelta(minutes=1), check_at=NOW + timedelta(minutes=2),
                   sentiment_at=NOW + timedelta(minutes=2), revision_at=NOW + timedelta(minutes=3))
        self.assertEqual(ps.compute_steps(row).next, "suggestions")
        self.assertFalse(ps.compute_steps(row).ready_to_send)

    def test_script_revision_omits_sentiment(self):
        steps = self.revision(content_type="VIDEO", script_stage=True, script_status="CHANGES_REQUESTED",
                              main_is_video=False, has_script=True, content_edited_at=NOW + timedelta(minutes=1),
                              check_at=NOW + timedelta(minutes=2), sentiment_at=None)
        self.assertTrue(steps.ready_to_send)
        self.assertEqual(steps.next, "send")

    def test_no_revision_timestamp_never_reuses_previous_check(self):
        row = post(status="CHANGES_REQUESTED")
        steps = ps.compute_steps(row)
        self.assertEqual((steps.next, steps.compliance, steps.sentiment), ("suggestions", "todo", "todo"))


class RevisionQueryTests(unittest.IsolatedAsyncioTestCase):
    async def test_only_returned_posts_need_revision_metadata(self):
        returned, draft = uuid4(), uuid4()
        with patch.object(revision_steps, "fetch_all", AsyncMock(return_value=[
            {"id": returned, "revision_at": NOW, "unresolved_comments": False}])) as read:
            result = await revision_steps.for_submissions([{"id": returned, "status": "CHANGES_REQUESTED"},
                                                         {"id": draft, "status": "DRAFT"}])
            self.assertEqual(read.await_args.args[1], [returned])
            self.assertEqual(result[returned]["revision_at"], NOW)
