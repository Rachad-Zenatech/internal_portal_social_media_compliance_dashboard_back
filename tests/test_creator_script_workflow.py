import io
import unittest
from types import SimpleNamespace
from uuid import uuid4
from unittest.mock import AsyncMock, patch
from zipfile import ZipFile
from xml.etree import ElementTree as ET

from fastapi import HTTPException
from models.compliance.script_review_model import ScriptReviseRequest
from services.compliance.evaluation import script_creator_workflow as workflow, script_review_service as review
from services.compliance.media.script_export import script_docx
from services.compliance.media.document_layout import docx_to_text
from services.compliance.submissions import delegation_service as delegation, submission_service as subs
from tools.compliance.submissions import script_review_router as router


class WordExportTests(unittest.TestCase):
    def test_latest_wording_tables_and_headings_round_trip(self):
        text = "# Filming script\nUpdated **approved wording** & rescue <demo>\n| Time | Visual | Voiceover |\n| 0:00 | Presenter | Revised wording |"
        data = script_docx(text)
        with ZipFile(io.BytesIO(data)) as doc:
            self.assertIsNone(doc.testzip())
            for name in doc.namelist():
                ET.fromstring(doc.read(name))
            self.assertIn(b"<w:tbl", doc.read("word/document.xml"))
        rendered = docx_to_text(data)
        for fragment in ("Filming script", "approved wording", "Revised wording", "rescue <demo>"):
            self.assertIn(fragment, rendered)

    def test_page_breaks_and_unicode(self):
        data = script_docx("First scene\n--- Page 2 ---\nScene two: Montr?al ??")
        self.assertIn("Scene two: Montr?al ??", docx_to_text(data))
        with ZipFile(io.BytesIO(data)) as doc:
            self.assertIn(b'w:type="page"', doc.read("word/document.xml"))


class CreatorRevisionTests(unittest.IsolatedAsyncioTestCase):
    async def test_approved_script_is_editable_only_before_a_video_exists(self):
        row = {"id": uuid4(), "script_status": "APPROVED"}
        with patch.object(subs, "fetch_one", AsyncMock(return_value=None)):
            self.assertTrue(await workflow.can_revise(row))
        with patch.object(subs, "fetch_one", AsyncMock(return_value={"exists": 1})):
            self.assertFalse(await workflow.can_revise(row))
        self.assertFalse(await workflow.can_revise({**row, "script_status": "IN_REVIEW"}))

    async def test_returned_scripts_stay_editable(self):
        for status in ("CHANGES_REQUESTED", "EDITS_PROPOSED"):
            self.assertTrue(await workflow.can_revise({"script_status": status}))

    async def test_edits_to_an_approved_script_go_back_to_review(self):
        creator, submission_id = uuid4(), uuid4()
        row = {"id": submission_id, "created_by": creator, "status": "DRAFT", "content_type": "VIDEO",
               "script_required": True, "script_status": "APPROVED"}
        with patch.object(subs, "_get_owned_row", AsyncMock(return_value=row)), \
             patch.object(subs, "fetch_one", AsyncMock(return_value=None)), \
             patch.object(review, "_versions", AsyncMock(return_value=[SimpleNamespace(body="Before")])), \
             patch.object(review, "_require_not_duplicate", AsyncMock()), \
             patch.object(review, "_send", AsyncMock(return_value="sent")) as send:
            self.assertEqual(await review.revise(submission_id, creator, ScriptReviseRequest(text="After")), "sent")
            self.assertEqual(send.await_args.args[1:4], ("After", "CREATOR_EDIT", creator))

    async def test_video_attached_prevents_revising_the_approved_script(self):
        row = {"id": uuid4(), "status": "DRAFT", "content_type": "VIDEO", "script_required": True, "script_status": "APPROVED"}
        with patch.object(subs, "_get_owned_row", AsyncMock(return_value=row)), \
             patch.object(subs, "fetch_one", AsyncMock(return_value={"exists": 1})):
            with self.assertRaises(HTTPException) as error:
                await review.revise(row["id"], uuid4(), ScriptReviseRequest(text="After"))
            self.assertEqual(error.exception.status_code, 409)


class ScriptDownloadTests(unittest.IsolatedAsyncioTestCase):
    async def test_download_exports_exact_saved_version(self):
        stage = SimpleNamespace(reference_code="SUB-1183", versions=[SimpleNamespace(version_number=2, body="Reviewed edit")])
        with patch.object(router.svc, "get_stage", AsyncMock(return_value=stage)) as load:
            result = await router.export_script(uuid4(), uuid4(), 2)
            load.assert_awaited_once()
            self.assertIn("Reviewed edit", docx_to_text(result.body))
            self.assertIn("v2.docx", result.headers["content-disposition"])

    async def test_unavailable_version_does_not_export_a_different_one(self):
        with patch.object(router.svc, "get_stage", AsyncMock(return_value=SimpleNamespace(versions=[]))):
            with self.assertRaises(HTTPException) as error:
                await router.export_script(uuid4(), uuid4(), 3)
            self.assertEqual(error.exception.status_code, 404)

    async def test_existing_read_permissions_are_enforced(self):
        with patch.object(router.svc, "get_stage", AsyncMock(side_effect=HTTPException(403, "Forbidden"))):
            with self.assertRaises(HTTPException) as error:
                await router.export_script(uuid4(), uuid4(), 1)
            self.assertEqual(error.exception.status_code, 403)


class VideoReviewerTests(unittest.TestCase):
    def test_video_keeps_script_reviewers_and_allows_additions(self):
        original, extra = uuid4(), uuid4()
        for status in ("APPROVED", "EDITS_PROPOSED", "CHANGES_REQUESTED"):
            kept, added = delegation.split_picked_reviewers({"status": "DRAFT", "script_status": status,
                                                          "reviewer_ids": [original]}, [extra])
            self.assertEqual(kept, [original])
            self.assertEqual(added, [extra])

    def test_fresh_draft_can_change_its_initial_reviewers(self):
        original, extra = uuid4(), uuid4()
        self.assertEqual(delegation.split_picked_reviewers({"status": "DRAFT", "reviewer_ids": [original]}, [extra]), ([], [extra]))
