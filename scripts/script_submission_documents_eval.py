"""Shared-document adoption and original-post sync regressions, using isolated mocked services."""
import importlib.util
import sys
import types
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, Mock, MagicMock, patch
from uuid import uuid4
from fastapi import HTTPException

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def load(name, path, modules):
    with patch.dict(sys.modules, modules):
        spec = importlib.util.spec_from_file_location(name, ROOT / path)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return mod


class SubmissionDocumentTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.owner, self.reviewer, self.submission, self.doc_id = (uuid4() for _ in range(4))
        self.row = dict(id=self.submission, created_by=self.owner, reviewer_ids=[self.reviewer], title="abc",
                        script_required=True, script_status="IN_REVIEW", details={}, updated_at=1)
        self.db = types.ModuleType("database")
        self.db.fetch_one = AsyncMock(return_value=None)
        self.conn = MagicMock()
        for name in ("fetchrow", "fetchval", "fetch", "execute", "executemany"):
            setattr(self.conn, name, AsyncMock())
        self.conn.transaction.return_value.__aenter__.return_value = self.conn
        self.pool = MagicMock()
        self.pool.acquire.return_value.__aenter__.return_value = self.conn
        self.db.get_pool = Mock(return_value=self.pool)
        self.shared = types.ModuleType("shared")
        self.shared.enabled = Mock(return_value=True)
        self.shared.SOURCE_SERVICE = "social-media"
        self.remote = types.ModuleType("remote")
        self.remote._emails = AsyncMock(return_value={self.owner: "owner@example.com", self.reviewer: "reviewer@example.com"})
        self.remote._call = AsyncMock(return_value=dict(document_id="shared-1", status="IN_REVIEW", round=1, text="canonical"))
        self.remote.body = AsyncMock(return_value=("<p>canonical</p>", "canonical"))
        self.subs = types.ModuleType("subs")
        self.subs._get_owned_row = AsyncMock(return_value=self.row)
        self.subs._can_review = AsyncMock(return_value=True)
        self.subs._add_event = AsyncMock()
        self.core = types.ModuleType("core")
        self.core._managers = AsyncMock(return_value=[])
        self.core._notify = AsyncMock()
        self.scripts = types.ModuleType("scripts")
        self.scripts._lock_status = AsyncMock()
        self.scripts._add_version = AsyncMock(return_value=1)
        self.scripts.get_stage = AsyncMock(return_value="stage")
        self.scripts.CREATOR_TURN = ("CHANGES_REQUESTED", "EDITS_PROPOSED")
        templates = types.ModuleType("templates")
        templates.shared_script_html = lambda number, title, text: "<p>" + text + "</p>"
        self.modules = {"postgresql_db.database": self.db,
            "services.compliance.evaluation.shared_script_service": self.shared,
            "services.compliance.evaluation.shared_script_review": self.remote,
            "services.compliance.submissions.submission_service": self.subs,
            "services.compliance.evaluation.script_doc_service": self.core,
            "services.compliance.evaluation.script_review_service": self.scripts,
            "services.compliance.evaluation.script_doc_templates": templates}
        self.bridge = load("bridge", "services/compliance/evaluation/script_submission_documents.py", self.modules)
        self.sync = load("sync", "services/compliance/evaluation/script_submission_sync.py", self.modules)
        content = types.ModuleType("content")
        content.submission_beats = lambda title, body: ({}, [{"voiceover": body}])
        self.modules["services.compliance.evaluation.script_submission_content"] = content
        self.patch = patch.dict(sys.modules, self.modules)
        self.patch.start()
        self.addCleanup(self.patch.stop)

    async def test_import_payload_preserves_owner_assigned_reviewers_and_stable_id(self):
        first, people, _ = await self.bridge._prepare(self.row, "First spoken line", 1, "IN_REVIEW")
        second, _, _ = await self.bridge._prepare(self.row, "First spoken line", 1, "IN_REVIEW")
        self.assertEqual(first, second)
        payload = self.remote._call.call_args.kwargs["json"]
        self.assertEqual(payload["user_email"], "owner@example.com")
        self.assertEqual(payload["invitees"], [{"email": "reviewer@example.com", "role": "REVIEWER", "capacity": "EDIT"}])
        self.assertEqual(payload["external_ref"]["submissionId"], str(self.submission))
        self.assertEqual(people, [self.reviewer])
        another = uuid4()
        self.remote._emails.return_value[another] = "another@example.com"
        await self.bridge._prepare({**self.row, "reviewer_ids": [another]}, "First spoken line", 1, "IN_REVIEW")
        changed = self.remote._call.call_args.kwargs["json"]
        self.assertNotEqual(payload["source_hash"], changed["source_hash"], "Changed assignments must not reuse old shared access")

    async def test_missing_creator_fails_before_remote_creation(self):
        with self.assertRaises(HTTPException):
            await self.bridge._prepare({**self.row, "created_by": None}, "body", 1, "IN_REVIEW")
        self.remote._call.assert_not_awaited()

    async def test_self_only_reviewer_fails_before_creating_shared_document(self):
        with self.assertRaises(HTTPException):
            await self.bridge._prepare({**self.row, "reviewer_ids": [self.owner]}, "body", 1, "IN_REVIEW")
        self.remote._call.assert_not_awaited()

    async def test_shared_failure_leaves_source_version_and_status_uncommitted(self):
        self.bridge.fetch_one = AsyncMock(side_effect=[None, {"n": 0}])
        self.remote._call.side_effect = HTTPException(502, "Shared server unavailable")
        with self.assertRaises(HTTPException):
            await self.bridge.send_checked(self.row, "body", "UPLOAD", self.owner, {})
        self.scripts._add_version.assert_not_awaited()
        self.db.get_pool.assert_not_called()

    async def test_adoption_authorizes_before_import(self):
        self.subs._get_owned_row.side_effect = HTTPException(404, "Not found")
        with self.assertRaises(HTTPException):
            await self.bridge.adopt(self.submission, self.reviewer)
        self.remote._call.assert_not_awaited()

    async def test_already_linked_upload_reuses_document(self):
        self.bridge.fetch_one = AsyncMock(return_value={"id": self.doc_id})
        self.assertEqual(await self.bridge.adopt(self.submission, self.reviewer), {"document_id": str(self.doc_id)})
        self.remote._call.assert_not_awaited()

    async def test_existing_upload_adopts_without_resubmitting_or_duplicating_source_version(self):
        self.bridge.fetch_one = AsyncMock(side_effect=[None, {"body": "original", "version_number": 1}])
        self.conn.fetchrow.side_effect = [self.row, None]
        self.conn.fetchval.return_value = self.doc_id
        result = await self.bridge.adopt(self.submission, self.reviewer)
        self.assertTrue(result["document_id"])
        self.remote._call.assert_awaited_once()
        self.scripts._add_version.assert_not_awaited()
        link = next(c for c in self.conn.execute.await_args_list if "INSERT INTO script_document_posts" in c.args[0])
        self.assertEqual(link.args[-1], self.submission)

    async def test_concurrent_source_change_aborts_before_remote_import(self):
        self.bridge.fetch_one = AsyncMock(side_effect=[None, {"body": "original", "version_number": 1}])
        self.conn.fetchrow.side_effect = [{**self.row, "updated_at": 2}, None]
        with self.assertRaises(HTTPException):
            await self.bridge.adopt(self.submission, self.reviewer)
        self.remote._call.assert_not_awaited()

    async def test_new_send_creates_linked_native_review_and_notifies_assigned_reviewer(self):
        self.bridge.fetch_one = AsyncMock(side_effect=[None, {"n": 0}])
        self.conn.fetchval.return_value = self.doc_id
        stage = await self.bridge.send_checked(self.row, "original", "UPLOAD", self.owner, {"score": 90})
        self.assertEqual(stage, "stage")
        self.scripts._add_version.assert_awaited_once()
        first_notice = self.core._notify.await_args_list[0]
        self.assertEqual(first_notice.args[0], [self.reviewer])
        self.assertEqual(str(first_notice.args[-1]), self.remote._call.call_args.kwargs["json"]["external_ref"]["id"])
        self.assertFalse(any("INSERT INTO content_submissions" in c.args[0] for c in self.conn.execute.await_args_list))

    async def test_legacy_editor_mode_does_not_create_shared_documents(self):
        self.shared.enabled.return_value = False
        self.assertEqual(await self.bridge.adopt(self.submission, self.reviewer), {"document_id": None})
        self.remote._call.assert_not_awaited()

    async def test_persist_links_existing_post_without_creating_another(self):
        got = dict(status="IN_REVIEW", round=1, text="canonical", document_id="shared-1")
        self.conn.fetchval.return_value = self.doc_id
        await self.bridge._persist(self.conn, self.row, self.doc_id, [self.reviewer], got)
        calls = self.conn.execute.await_args_list
        link = next(c for c in calls if "INSERT INTO script_document_posts" in c.args[0])
        self.assertEqual(link.args[-1], self.submission)
        self.assertFalse(any("INSERT INTO content_submissions" in c.args[0] for c in calls))
        self.assertEqual(self.conn.executemany.call_args.args[1], [(self.doc_id, self.reviewer, 0)])

    async def test_repeat_persistence_does_not_reset_reviewers_or_add_versions(self):
        self.conn.fetchval.return_value = None
        await self.bridge._persist(self.conn, self.row, self.doc_id, [], dict(text="canonical", status="APPROVED", round=1, document_id="shared-1"))
        self.conn.executemany.assert_not_awaited()
        self.assertFalse(any("script_document_versions" in c.args[0] for c in self.conn.execute.await_args_list))

    def source_document(self):
        return dict(id=self.doc_id, created_by=self.owner, title="abc", position=1, status="APPROVED", body_text="canonical",
                    body_html="<p>canonical</p>", imported_body="canonical", submission_id=self.submission, decided_by=self.reviewer,
                    details={"source_submission_id": str(self.submission)}, decision_note="Approved")

    async def test_unchanged_approval_keeps_original_source_version_and_score(self):
        self.sync.fetch_one = AsyncMock(return_value=self.source_document())
        self.conn.fetchrow.side_effect = [{"script_status": "IN_REVIEW"}, {"version_number": 1, "body": "original upload formatting"}]
        self.conn.fetch.return_value = [{"user_id": self.reviewer}]
        await self.sync.sync_document(self.doc_id)
        calls = self.conn.execute.await_args_list
        self.assertFalse(any("INSERT INTO content_script_versions" in c.args[0] for c in calls))
        update = next(c for c in calls if "UPDATE content_submissions" in c.args[0])
        self.assertEqual(update.args[2], "APPROVED")
        self.assertFalse(update.args[-1])

    async def test_real_shared_edit_versions_approved_text_on_original_post(self):
        self.sync.fetch_one = AsyncMock(return_value=self.source_document())
        self.remote.body.return_value = ("<p>Real edit</p>", "Real edit")
        self.conn.fetchrow.side_effect = [{"script_status": "IN_REVIEW"}, {"version_number": 1, "body": "original"}]
        self.conn.fetch.return_value = [{"user_id": self.reviewer}]
        await self.sync.sync_document(self.doc_id)
        calls = self.conn.execute.await_args_list
        version = next(c for c in calls if "INSERT INTO content_script_versions" in c.args[0])
        self.assertEqual(version.args[1:5], (self.submission, 2, "Real edit", "REVIEWER_EDIT"))
        self.assertFalse(any("INSERT INTO content_submissions" in c.args[0] for c in calls))

    async def test_native_template_post_is_not_overwritten_by_adoption_sync(self):
        self.sync.fetch_one = AsyncMock(return_value={**self.source_document(), "details": {}})
        await self.sync.sync_document(self.doc_id)
        self.remote.body.assert_not_awaited()
        self.db.get_pool.assert_not_called()


if __name__ == "__main__":
    unittest.main(verbosity=2)
