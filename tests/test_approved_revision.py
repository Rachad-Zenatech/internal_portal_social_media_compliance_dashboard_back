import unittest
from datetime import datetime, timezone, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from fastapi import HTTPException
from models.compliance.submission_model import ReviewDecisionRequest
from services.compliance.submissions import approved_revision as flow

NOW = datetime(2026, 11, 26, 16, tzinfo=timezone.utc)

class ApprovedRevisionTests(unittest.IsolatedAsyncioTestCase):
    def row(self):
        return dict(id="post", current_version=1, status="CHANGES_REQUESTED", publish_after_changes=True,
                    title="Title", platforms=["FACEBOOK"], content_edited_at=NOW, media_edited_at=NOW,
                    approval_needed_by=NOW - timedelta(hours=1))

    def test_approval_allows_creator_revision_without_changing_legacy_approval(self):
        request = ReviewDecisionRequest(decision="APPROVE", publish_after_changes=True)
        self.assertEqual(flow.approval_outcome(self.row(), request, NOW)[1], "CHANGES_REQUESTED")
        legacy = ReviewDecisionRequest(decision="APPROVE", publish_now=True)
        self.assertEqual(flow.approval_outcome(self.row(), legacy, NOW)[1], "APPROVED")

    def test_reviewed_basics_cannot_change_but_text_sources_and_schedule_can(self):
        row = self.row()
        flow.require_locked_basics(row, dict(title="Title", caption_text="New caption", source_links=["source"], publish_at=NOW))
        with self.assertRaises(HTTPException): flow.require_locked_basics(row, dict(title="Different"))
        with self.assertRaises(HTTPException): flow.require_locked_basics(row, dict(platforms=["INSTAGRAM"]))
        flow.require_locked_basics({**row, "status":"DRAFT"}, dict(title="Different"))

    def test_singular_legacy_basics_can_be_saved_without_false_changes(self):
        row = {**self.row(), "brand_code":"ZENA", "account_id":"account", "audience":"GENERAL"}
        flow.require_locked_basics(row, dict(brand_codes=["ZENA"], account_ids=["account"], audiences=["GENERAL"]))

    def test_final_flow_progress_needs_fresh_checks_but_no_mandatory_edit(self):
        from services.compliance.submissions.revision_steps import compute
        row = {**self.row(), "revision_at":NOW, "check_at":NOW-timedelta(minutes=1), "sentiment_at":NOW-timedelta(minutes=1)}
        self.assertFalse(compute(row, []).ready_to_send)
        row.update(check_at=NOW+timedelta(minutes=1), sentiment_at=NOW+timedelta(minutes=1))
        self.assertTrue(compute(row, []).ready_to_send)

    async def test_old_compliance_or_sentiment_cannot_publish(self):
        state = dict(returned_at=NOW, sentiment_at=NOW + timedelta(minutes=2))
        with patch.object(flow, "fetch_one", AsyncMock(return_value=state)):
            with self.assertRaisesRegex(HTTPException, "compliance"):
                await flow.require_fresh_checks(self.row(), dict(checked_at=(NOW-timedelta(minutes=1)).isoformat()))
        state["sentiment_at"] = NOW - timedelta(minutes=1)
        with patch.object(flow, "fetch_one", AsyncMock(return_value=state)):
            with self.assertRaisesRegex(HTTPException, "sentiment"):
                await flow.require_fresh_checks(self.row(), dict(checked_at=NOW.isoformat()))

    async def test_fresh_final_checks_allow_publishing(self):
        state = dict(returned_at=NOW, sentiment_at=NOW + timedelta(minutes=2))
        with patch.object(flow, "fetch_one", AsyncMock(return_value=state)):
            await flow.require_fresh_checks(self.row(), dict(checked_at=(NOW+timedelta(minutes=1)).isoformat()))

    async def test_edit_after_checks_requires_another_check(self):
        state = dict(returned_at=NOW, sentiment_at=NOW + timedelta(minutes=2))
        row = {**self.row(), "content_edited_at":NOW+timedelta(minutes=3)}
        with patch.object(flow, "fetch_one", AsyncMock(return_value=state)):
            with self.assertRaises(HTTPException): await flow.require_fresh_checks(row, dict(checked_at=NOW.isoformat()))

    async def test_unchanged_content_reuses_checks_before_approval_and_deadline(self):
        from services.compliance.submissions.revision_steps import compute
        row = {**self.row(), "content_edited_at": NOW-timedelta(days=2),
               "media_edited_at": NOW-timedelta(days=3), "approval_needed_by": NOW+timedelta(days=1),
               "revision_at": NOW, "check_at": NOW-timedelta(minutes=1),
               "sentiment_at": NOW-timedelta(days=1)}
        with patch.object(flow, "fetch_one", AsyncMock(return_value={"sentiment_at": row["sentiment_at"]})):
            await flow.require_fresh_checks(row, {"checked_at": row["check_at"].isoformat()})
        self.assertTrue(compute(row, []).ready_to_send)

    async def test_new_media_still_requires_fresh_checks(self):
        row = {**self.row(), "media_edited_at": NOW+timedelta(minutes=1)}
        with patch.object(flow, "fetch_one", AsyncMock(return_value={"sentiment_at": NOW})):
            with self.assertRaisesRegex(HTTPException, "compliance"):
                await flow.require_fresh_checks(row, {"checked_at": NOW.isoformat()})

    async def test_ordinary_request_changes_does_not_grant_publishing(self):
        with patch.object(flow, "fetch_one", AsyncMock()) as read:
            await flow.require_fresh_checks({**self.row(), "publish_after_changes":False}, {})
            read.assert_not_awaited()


class ApprovedRevisionHandoffTests(unittest.IsolatedAsyncioTestCase):
    async def test_approve_records_permission_and_returns_editable_post(self):
        from services.compliance.submissions import submission_service as subs
        from services.compliance.submissions import caption_review_service, review_handoff, review_approval_service
        now = datetime.now(timezone.utc)
        row = dict(id="post", created_by="creator", current_version=1, status="IN_REVIEW", content_type="SOCIAL_POST",
                   latest_check={"manual_review_rules":[{"rule_number":"Rule 18","title":"Substantiate claims"}]}, publish_at=now+timedelta(days=2),
                   approval_needed_by=now-timedelta(hours=1), reference_code="SUB-TEST", title="Post")
        conn = SimpleNamespace(execute=AsyncMock())
        class Context:
            async def __aenter__(self): return conn
            async def __aexit__(self, *args): pass
        conn.transaction = Context
        pool = SimpleNamespace(acquire=Context)
        with patch.object(subs, "_get_owned_row", AsyncMock(return_value=row)), \
             patch.object(review_handoff, "require_reviewer", AsyncMock(return_value=False)), \
             patch.object(review_approval_service, "require_approval", AsyncMock(return_value=(row,100))) as gate, \
             patch.object(caption_review_service, "decision_status", AsyncMock(return_value="IN_REVIEW")), \
             patch.object(review_handoff, "require_undecided", AsyncMock()), \
             patch.object(subs, "get_pool", return_value=pool), \
             patch.object(subs, "_add_event", AsyncMock()), \
             patch.object(subs, "log_audit_event", AsyncMock()), \
             patch.object(review_handoff, "notify_now", AsyncMock()), \
             patch.object(subs.review_memory_service, "remember_soon"), \
             patch.object(subs, "get_submission", AsyncMock(return_value="returned")):
            result = await subs.decide_review("post", "reviewer", ReviewDecisionRequest(decision="APPROVE", publish_after_changes=True))
        gate.assert_awaited_once()
        self.assertEqual(result, "returned")
        job, submission = conn.execute.await_args_list
        self.assertEqual(job.args[3:5], ("CHANGES_REQUESTED", "APPROVE"))
        import json
        self.assertEqual(json.loads(job.args[7]), [], "Do not record confirmations the reviewer never made")
        self.assertEqual(submission.args[2], "CHANGES_REQUESTED")
        self.assertTrue(submission.args[4])

    async def test_delayed_approval_keeps_creator_revision_permission_at_release(self):
        from services.compliance.submissions import review_handoff_release as release, submission_service as subs
        row = dict(id="post", created_by="reviewer", decided_by="reviewer", current_version=1, decision="APPROVE",
                   outcome={"publish_after_changes":True}, publish_after_changes=True, publish_at=NOW+timedelta(days=1))
        conn = SimpleNamespace(execute=AsyncMock())
        with patch.object(subs, "_add_event", AsyncMock()):
            await release.release_one(conn, row, NOW)
        saved = conn.execute.await_args_list[0].args
        self.assertEqual(saved[2], "CHANGES_REQUESTED")
        self.assertTrue(saved[5])

    def test_clean_approval_locks_content_without_requiring_publish_date(self):
        row = {"status": "IN_REVIEW", "publish_at": None}
        request = ReviewDecisionRequest(decision="APPROVE", publish_after_changes=False)
        planned, status, label = flow.approval_outcome(row, request, NOW)
        self.assertIsNone(planned)
        self.assertEqual(status, "APPROVED")
        self.assertIn("without changes", label)

    async def test_clean_approval_never_requests_new_checks(self):
        row = {"status": "APPROVED", "publish_after_changes": False}
        with patch.object(flow, "fetch_one", AsyncMock()) as read:
            await flow.require_fresh_checks(row, {})
            read.assert_not_awaited()

    def test_approved_content_cannot_be_changed_through_creator_write_endpoints(self):
        from services.compliance.submissions.submission_service import _require_editable
        with self.assertRaises(HTTPException) as error:
            _require_editable({"status": "APPROVED"})
        self.assertEqual(error.exception.status_code, 409)
