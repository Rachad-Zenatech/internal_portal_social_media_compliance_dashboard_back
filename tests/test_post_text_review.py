"""Text-only submission gates and safe, attributed edits by multiple reviewers."""
import json
import unittest
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

from fastapi import HTTPException
from pydantic import ValidationError

from models.compliance.post_text_review_model import PostTextSave
from models.compliance.submission_model import SubmissionCreate, SubmissionUpdate
from services.compliance.submissions import post_text_review as review
from services.compliance.submissions import submission_service as subs
from services.compliance.submissions import media_freshness, submission_fields


class TextReviewTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.creator, self.first, self.second = uuid4(), uuid4(), uuid4()
        self.row = dict(id=uuid4(), content_type="TEXT_POST", caption_text="Original post text",
                        created_by=self.creator, status="IN_REVIEW", current_version=1,
                        reviewer_ids=[self.first, self.second], brand_code="ZENATECH", brand_codes=["ZENATECH"],
                        content_edited_at=None, latest_check={"score": 100})
        self.events = []
        self.conn = MagicMock()
        self.conn.fetchrow = AsyncMock(side_effect=lambda *args: dict(self.row))
        self.conn.fetch = AsyncMock(side_effect=lambda *args: list(self.events))
        self.conn.fetchval = AsyncMock(return_value="Reviewer One")
        self.conn.execute = AsyncMock(side_effect=self.execute)
        self.conn.transaction.return_value.__aenter__.return_value = None
        pool = MagicMock()
        pool.acquire.return_value.__aenter__.return_value = self.conn
        patches = [patch.object(review, "get_pool", return_value=pool),
                   patch.object(subs, "_get_owned_row", AsyncMock(side_effect=lambda *args, **kw: dict(self.row))),
                   patch.object(subs, "_can_suggest", AsyncMock(return_value=True)),
                   patch.object(subs, "_is_super_admin", AsyncMock(return_value=False)),
                   patch.object(subs, "_add_event", AsyncMock(side_effect=self.add_event))]
        for item in patches:
            item.start()
            self.addCleanup(item.stop)

    def execute(self, query, *args):
        if "UPDATE content_submissions SET caption_text" in query:
            self.row.update(caption_text=args[1], caption_html=args[2], latest_check=None,
                            content_edited_at=datetime.now(timezone.utc))
        return "UPDATE 1"

    def add_event(self, conn, submission_id, event_type, message, actor, data):
        self.assertEqual(event_type, "CAPTION_EDITED")
        self.assertEqual(data["before"], data["before_text"])
        self.assertEqual(data["after"], data["after_text"])
        self.events.append(dict(id=str(len(self.events) + 1), reviewer_id=actor,
                                reviewer_name=data["reviewer_name"], created_at=datetime.now(timezone.utc),
                                data=json.dumps(data)))

    def request(self, text, state=None):
        return PostTextSave(text=text, base_text=state.text if state else "Original post text",
                            base_revision=state.revision if state else None)

    async def test_two_reviewers_save_attributed_changes_on_the_same_post(self):
        first = await review.save_review(self.row["id"], self.first, self.request("First reviewer text"))
        self.conn.fetchval.return_value = "Reviewer Two"
        second = await review.save_review(self.row["id"], self.second, self.request("Second reviewer text", first))
        self.assertEqual(second.text, "Second reviewer text")
        self.assertEqual([edit.reviewer_id for edit in second.edits], [self.first, self.second])
        self.assertEqual(second.edits[1].before_text, first.text)
        self.assertEqual(second.edits[1].reviewer_name, "Reviewer Two")
        self.assertEqual(second.edits[0].version, 1)
        self.assertIsNone(self.row["latest_check"])
        self.assertEqual(self.row["status"], "IN_REVIEW")
        self.assertEqual(self.row["reviewer_ids"], [self.first, self.second])

    async def test_stale_save_never_overwrites_another_reviewers_work(self):
        await review.save_review(self.row["id"], self.first, self.request("First reviewer text"))
        with self.assertRaises(HTTPException) as caught:
            await review.save_review(self.row["id"], self.second, self.request("Second reviewer text"))
        self.assertEqual(caught.exception.status_code, 409)
        self.assertEqual(self.row["caption_text"], "First reviewer text")
        self.assertEqual(len(self.events), 1)

    async def test_revision_detects_changes_even_when_text_returns_to_original(self):
        first = await review.save_review(self.row["id"], self.first, self.request("First reviewer text"))
        await review.save_review(self.row["id"], self.first, self.request("Original post text", first))
        with self.assertRaises(HTTPException) as caught:
            await review.save_review(self.row["id"], self.second, self.request("Stale draft"))
        self.assertEqual(caught.exception.status_code, 409)

    async def test_creators_and_nonreviewers_cannot_edit_in_review(self):
        self.assertFalse(await review.may_edit(self.row, self.creator))
        with patch.object(subs, "_can_suggest", AsyncMock(return_value=False)):
            with self.assertRaises(HTTPException) as caught:
                await review.save_review(self.row["id"], self.first, self.request("Changed"))
        self.assertEqual(caught.exception.status_code, 403)
        self.assertFalse(self.events)

    async def test_closed_review_and_other_post_types_reject_edits(self):
        for state in ("APPROVED", "REJECTED", "CHANGES_REQUESTED", "DRAFT"):
            self.row["status"] = state
            with self.assertRaises(HTTPException):
                await review.save_review(self.row["id"], self.first, self.request("Changed"))
        self.row.update(status="IN_REVIEW", content_type="SOCIAL_POST")
        with self.assertRaises(HTTPException):
            await review.save_review(self.row["id"], self.first, self.request("Changed"))
        self.assertFalse(self.events)

    async def test_permission_scope_is_checked_before_reading_history_or_saving(self):
        with patch.object(subs, "_get_owned_row", AsyncMock(side_effect=HTTPException(404, "Not found"))):
            for call in (review.get_review(self.row["id"], self.second),
                         review.save_review(self.row["id"], self.second, self.request("Changed"))):
                with self.assertRaises(HTTPException) as caught:
                    await call
                self.assertEqual(caught.exception.status_code, 404)
        self.conn.fetch.assert_not_awaited()

    async def test_creator_can_read_attributed_history(self):
        await review.save_review(self.row["id"], self.first, self.request("Reviewed text"))
        state = await review.get_review(self.row["id"], self.creator)
        self.assertFalse(state.can_edit)
        self.assertEqual(state.edits[0].after_text, state.text)

    async def test_unchanged_save_does_not_invalidate_checks_or_make_history(self):
        await review.save_review(self.row["id"], self.first, self.request("Original post text"))
        self.assertFalse(self.events)
        self.assertEqual(self.row["latest_check"], {"score": 100})

    async def test_text_is_escaped_when_mirrored_to_caption_html(self):
        await review.save_review(self.row["id"], self.first, self.request('<script>alert(1)</script>\nText & more'))
        self.assertNotIn("<script>", self.row["caption_html"])
        self.assertIn("&lt;script&gt;", self.row["caption_html"])
        self.assertIn("<br>", self.row["caption_html"])

    async def test_approval_of_stale_text_or_check_is_rejected_under_lock(self):
        original = dict(self.row)
        for field, value in (("caption_text", "Changed"), ("latest_check", None),
                             ("content_edited_at", datetime.now(timezone.utc))):
            current = {**original, field: value}
            self.conn.fetchrow.return_value = current
            self.conn.fetchrow.side_effect = None
            with self.assertRaises(HTTPException) as caught:
                await review.lock_decision(self.conn, original)
            self.assertEqual(caught.exception.status_code, 409)

    async def test_normal_social_post_decisions_keep_existing_status_behavior(self):
        self.assertEqual(await review.lock_decision(self.conn, {**self.row, "content_type": "SOCIAL_POST"}), "IN_REVIEW")

    async def test_text_post_uploads_and_switch_with_existing_files_are_rejected(self):
        with self.assertRaises(HTTPException):
            await review.lock_upload(self.conn, self.row["id"])
        self.conn.fetchval.return_value = True
        with self.assertRaises(HTTPException):
            await submission_fields.lock_script_mode(self.conn, self.row["id"], {"content_type": "TEXT_POST"})
        self.conn.fetchval.return_value = False
        await submission_fields.lock_script_mode(self.conn, self.row["id"], {"content_type": "TEXT_POST"})

    def test_text_type_is_accepted_by_create_and_update(self):
        self.assertEqual(SubmissionCreate(title="Text", content_type="TEXT_POST").content_type, "TEXT_POST")
        self.assertEqual(SubmissionUpdate(content_type="TEXT_POST").content_type, "TEXT_POST")
        self.assertNotIn("TEXT_POST", subs.MAIN_FILE_REQUIRED)

    def test_blank_and_oversized_review_text_is_rejected(self):
        for text in ("", " \n\t", "x" * 100001):
            with self.assertRaises(ValidationError):
                self.request(text)

    def test_edit_makes_the_old_compliance_check_stale(self):
        now = datetime.now(timezone.utc)
        self.assertFalse(media_freshness.is_current({"media_edited_at": now}, {"checked_media_edited_at": None}))
        self.assertIn("s.content_type = 'TEXT_POST' THEN s.content_edited_at", media_freshness.REVISION_SQL)

    def test_changed_assignments_are_rejected(self):
        with self.assertRaises(HTTPException) as caught:
            review.require_same_assignment(self.row, {**self.row, "reviewer_ids": [self.first]})
        self.assertEqual(caught.exception.status_code, 409)

    async def test_assigned_text_reviewers_can_run_the_existing_check(self):
        row = await review.check_row(self.row["id"], self.first)
        self.assertEqual(row["id"], self.row["id"])
        subs._get_owned_row.assert_awaited_once_with(self.row["id"], self.first, manager_can_read=True)

    async def test_recheck_does_not_grant_nonreviewers_owner_access(self):
        with patch.object(subs, "_can_suggest", AsyncMock(return_value=False)):
            await review.check_row(self.row["id"], self.second)
        self.assertEqual(subs._get_owned_row.await_count, 2)
        subs._get_owned_row.assert_awaited_with(self.row["id"], self.second)

    async def test_check_results_raced_by_an_edit_are_not_saved(self):
        check = {"checked_at": datetime.now(timezone.utc).isoformat()}
        with patch.object(media_freshness, "check_still_current", AsyncMock(return_value=False)):
            with self.assertRaises(HTTPException) as caught:
                await review.store_check(self.conn, self.row, check)
        self.assertEqual(caught.exception.status_code, 409)
        self.conn.execute.assert_not_awaited()

    async def test_current_check_results_can_be_saved(self):
        check = {"checked_at": datetime.now(timezone.utc).isoformat(), "score": 100}
        with patch.object(media_freshness, "check_still_current", AsyncMock(return_value=True)):
            await review.store_check(self.conn, self.row, check)
        self.conn.execute.assert_awaited_once()
