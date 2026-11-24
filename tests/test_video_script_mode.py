"""Optional script review must not bypass video checks or an active review."""

import json
import unittest
from unittest.mock import AsyncMock, patch
from uuid import uuid4

from fastapi import HTTPException

from models.compliance.submission_model import SubmissionCreate, SubmissionUpdate
from services.compliance.submissions import submission_fields, submission_service as subs


def draft(**changes):
    row = {
        "id": uuid4(), "content_type": "VIDEO", "script_required": True,
        "script_status": "DRAFT", "current_version": 0, "status": "DRAFT",
        "title": "Prepared video", "brand_code": "TEST", "audience": "GENERAL",
        "region": "Global", "platforms": ["YOUTUBE"], "account_ids": [uuid4()],
        "rule_sets": ["Public Company Disclosure"], "approval_needed_by": "tomorrow",
        "reviewer_ids": [uuid4()], "caption_text": "Video caption",
    }
    row.update(changes)
    return row


class VideoScriptModeTests(unittest.TestCase):
    def test_requests_accept_explicit_bypass_without_changing_default(self):
        self.assertIsNone(SubmissionCreate(title="Video", content_type="VIDEO").script_required)
        self.assertFalse(SubmissionCreate(title="Video", content_type="VIDEO", script_required=False).script_required)
        self.assertEqual(SubmissionUpdate(script_required=False).model_dump(exclude_unset=True)["script_required"], False)

    def test_bypass_is_saved_and_normal_autosave_does_not_reenable_it(self):
        self.assertEqual(submission_fields.editable_type_fields(draft(), {"script_required": False}), {"script_required": False})
        fields = submission_fields.editable_type_fields(draft(script_required=False), {"content_type": "VIDEO", "title": "Edited"})
        self.assertNotIn("script_required", fields)

    def test_switch_to_video_still_defaults_to_script_review(self):
        fields = submission_fields.editable_type_fields(draft(content_type="IMAGE", script_required=False), {"content_type": "VIDEO"})
        self.assertTrue(fields["script_required"])

    def test_switch_to_video_can_explicitly_skip_script_review(self):
        fields = submission_fields.editable_type_fields(draft(content_type="IMAGE", script_required=False), {"content_type": "VIDEO", "script_required": False})
        self.assertFalse(fields["script_required"])

    def test_checkbox_can_be_unchecked_before_review(self):
        self.assertEqual(submission_fields.script_mode_fields(draft(script_required=False), {"script_required": True}), {"script_required": True})

    def test_cannot_bypass_active_or_completed_review(self):
        for status in ("IN_REVIEW", "APPROVED", "CHANGES_REQUESTED", "EDITS_PROPOSED"):
            with self.subTest(status=status), self.assertRaises(HTTPException) as raised:
                submission_fields.script_mode_fields(draft(script_status=status), {"script_required": False})
            self.assertEqual(raised.exception.status_code, 409)

    def test_cannot_bypass_a_sent_post_or_background_script_send(self):
        for changes in ({"current_version": 1}, {"status": "IN_REVIEW"}, {"script_send": {"status": "PROCESSING"}}, {"script_send": json.dumps({"status": "PROCESSING"})}):
            with self.subTest(changes=changes), self.assertRaises(HTTPException):
                submission_fields.script_mode_fields(draft(**changes), {"script_required": False})

    def test_other_post_types_ignore_script_mode(self):
        self.assertEqual(submission_fields.script_mode_fields(draft(content_type="IMAGE"), {"script_required": False}), {})

    def test_video_upload_gate_respects_bypass_and_original_flow(self):
        with self.assertRaises(HTTPException):
            subs._require_script_stage_allows(draft(), "MAIN")
        subs._require_script_stage_allows(draft(script_required=False), "MAIN")
        subs._require_script_stage_allows(draft(script_status="APPROVED"), "MAIN")


class VideoScriptModeAsyncTests(unittest.IsolatedAsyncioTestCase):
    async def test_bypass_requires_video_caption_and_normal_submission_details(self):
        with patch.object(subs, "fetch_one", new=AsyncMock(return_value=None)), patch.object(subs.image_provenance_service, "missing", return_value=False):
            missing = await subs._missing_for_submit(draft(script_required=False, caption_text="", reviewer_ids=[]))
        self.assertIn("main_file", missing)
        self.assertIn("caption", missing)
        self.assertIn("reviewer_ids", missing)
        self.assertNotIn("script_approval", missing)

    async def test_script_approval_remains_required_by_default(self):
        with patch.object(subs, "fetch_one", new=AsyncMock(return_value={"ok": 1})), patch.object(subs.image_provenance_service, "missing", return_value=False):
            missing = await subs._missing_for_submit(draft())
        self.assertIn("script_approval", missing)

    async def test_rechecks_mode_under_lock_if_review_started_after_initial_read(self):
        conn = AsyncMock()
        conn.fetchrow.return_value = draft(script_status="IN_REVIEW")
        with self.assertRaises(HTTPException):
            await submission_fields.lock_script_mode(conn, uuid4(), {"script_required": False})
        self.assertIn("FOR UPDATE", conn.fetchrow.call_args.args[0])


if __name__ == "__main__":
    unittest.main()
