"""Script hub visibility and submitted-upload regression checks; no database or external services."""
import importlib.util
import sys
import types
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def load_service(name, path, modules):
    with patch.dict(sys.modules, modules):
        spec = importlib.util.spec_from_file_location(name, ROOT / path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module


class ScriptHubTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.user, self.creator, self.submission = uuid4(), uuid4(), uuid4()
        self.now = datetime.now(timezone.utc)
        self.subs = types.ModuleType("submission_service")
        for name, value in (("_is_super_admin", False), ("_can_see_team_content", True),
                            ("_can_review", True), ("_can_manage_team_content", True), ("_brand_scope", ["DEFENSE"])):
            setattr(self.subs, name, AsyncMock(return_value=value))
        self.subs._reviewer_sees = Mock(return_value="EXISTING_SOURCE_VISIBILITY")
        database = types.ModuleType("database")
        database.fetch_all = AsyncMock(return_value=[])
        self.modules = {"services.compliance.submissions.submission_service": self.subs,
                        "postgresql_db.database": database}
        self.hub = load_service("hub_under_test", "services/compliance/evaluation/script_hub_submissions.py", self.modules)
        self.hub.fetch_all = AsyncMock(return_value=[self.row()])

    def row(self, **extra):
        return {"id": self.submission, "title": "abc", "reference_code": "SUB-1135", "created_by": self.creator,
                "script_status": "IN_REVIEW", "creator_name": "Creator", "reviewer_names": ["Assigned reviewer"],
                "version_number": 1, "updated_at": self.now, "created_at": self.now, **extra}

    async def test_assigned_reviewers_get_original_submission_and_only_assigned_sql(self):
        [item] = await self.hub.list_submission_scripts(self.user, "review")
        query, *args = self.hub.fetch_all.call_args.args
        self.assertIn("AND ($1::uuid = ANY(s.reviewer_ids))", query)
        self.assertEqual(args, [self.user])
        self.assertEqual(item.submission_id, self.submission)
        self.assertEqual(item.reference_code, "SUB-1135")
        self.assertEqual(item.status, "IN_REVIEW")
        self.assertTrue(item.needs_you)
        self.assertEqual(item.required, 0)

    async def test_super_admin_all_is_not_restricted_by_assignment_or_brand(self):
        self.subs._is_super_admin.return_value = True
        [item] = await self.hub.list_submission_scripts(self.user, "all")
        query, *args = self.hub.fetch_all.call_args.args
        self.assertIn("AND (TRUE)", query)
        self.assertEqual(args, [])
        self.subs._brand_scope.assert_not_awaited()
        self.assertEqual(item.title, "abc")

    async def test_super_admin_sent_to_me_still_uses_assignment(self):
        self.subs._is_super_admin.return_value = True
        await self.hub.list_submission_scripts(self.user, "review")
        self.assertIn("AND ($1::uuid = ANY(s.reviewer_ids))", self.hub.fetch_all.call_args.args[0])

    async def test_other_managers_reuse_source_submission_visibility(self):
        await self.hub.list_submission_scripts(self.user, "all")
        query, *args = self.hub.fetch_all.call_args.args
        self.assertIn("s.created_by = $1 OR EXISTING_SOURCE_VISIBILITY", query)
        self.assertEqual(args, [self.user, ["DEFENSE"]])
        self.subs._reviewer_sees.assert_called_once_with("$1", "$2")

    async def test_unprivileged_users_do_not_read_submission_rows(self):
        self.subs._can_see_team_content.return_value = False
        self.assertEqual(await self.hub.list_submission_scripts(self.user, "review"), [])
        self.hub.fetch_all.assert_not_awaited()

    async def test_creator_scope_only_queries_own_uploads(self):
        rows = await self.hub.list_submission_scripts(self.user, "mine")
        self.assertEqual(len(rows), 1)
        sql, *args = self.hub.fetch_all.await_args.args
        self.assertIn("s.created_by = $1", sql)
        self.assertEqual(args, [self.user])
        self.subs._can_see_team_content.assert_not_awaited()

    async def test_uploaded_edits_proposed_preserves_status_and_creator_turn(self):
        self.hub.fetch_all.return_value = [self.row(created_by=self.user, script_status="EDITS_PROPOSED")]
        [item] = await self.hub.list_submission_scripts(self.user, "all")
        self.assertEqual(item.status, "CHANGES_REQUESTED")
        self.assertEqual(item.source_status, "EDITS_PROPOSED")
        self.assertTrue(item.needs_you)

    async def test_creator_cannot_review_own_script_except_super_admin(self):
        self.hub.fetch_all.return_value = [self.row(created_by=self.user)]
        [ordinary] = await self.hub.list_submission_scripts(self.user, "review")
        self.assertFalse(ordinary.needs_you)
        self.subs._is_super_admin.return_value = True
        [admin] = await self.hub.list_submission_scripts(self.user, "review")
        self.assertTrue(admin.needs_you)

    async def test_legacy_upload_without_creator_does_not_break_admin_listing(self):
        self.subs._is_super_admin.return_value = True
        self.hub.fetch_all.return_value = [self.row(created_by=None, creator_name=None)]
        [item] = await self.hub.list_submission_scripts(self.user, "all")
        self.assertIsNone(item.creator_id)
        self.assertEqual(item.creator_name, "Unknown creator")

    async def test_only_sent_active_scripts_are_queried_and_document_posts_are_deduplicated(self):
        await self.hub.list_submission_scripts(self.user, "review")
        query = self.hub.fetch_all.call_args.args[0]
        for required in ("JOIN LATERAL", "content_script_versions", "s.deleted_at IS NULL", "s.script_required",
                         "s.content_type = 'VIDEO'", "s.script_status IN ('IN_REVIEW', 'CHANGES_REQUESTED', 'EDITS_PROPOSED', 'APPROVED')",
                         "NOT EXISTS", "script_document_posts", "d.deleted_at IS NULL"):
            self.assertIn(required, query)

    async def test_hub_combines_existing_documents_and_uploads_sorted_by_update(self):
        modules = {**self.modules, "services.compliance.evaluation.script_doc_service": types.ModuleType("core"),
                   "services.compliance.evaluation.script_review_service": types.ModuleType("scripts"),
                   "services.compliance.evaluation.script_hub_submissions": self.hub}
        reads = load_service("reads_under_test", "services/compliance/evaluation/script_doc_reads.py", modules)
        reads.fetch_all = AsyncMock(return_value=[{
            "id": uuid4(), "title": "Existing document", "template_key": "blank", "status": "DRAFT", "round": 0,
            "created_by": self.creator, "creator_name": "Creator", "reviewer_names": [], "required": 0, "checked": 0,
            "awaiting_me": False, "updated_at": self.now - timedelta(days=1), "created_at": self.now - timedelta(days=1),
        }])
        items = await reads.list_docs(self.user, "all")
        self.assertEqual([item.title for item in items], ["abc", "Existing document"])
        self.assertIsNone(items[1].submission_id)

    async def test_all_scope_rejects_non_managers_before_querying(self):
        from fastapi import HTTPException
        modules = {**self.modules, "services.compliance.evaluation.script_doc_service": types.ModuleType("core"),
                   "services.compliance.evaluation.script_review_service": types.ModuleType("scripts"),
                   "services.compliance.evaluation.script_hub_submissions": self.hub}
        reads = load_service("reads_denied", "services/compliance/evaluation/script_doc_reads.py", modules)
        self.subs._can_manage_team_content.return_value = False
        with self.assertRaises(HTTPException) as error:
            await reads.list_docs(self.user, "all")
        self.assertEqual(error.exception.status_code, 403)
        self.hub.fetch_all.assert_not_awaited()


if __name__ == "__main__":
    unittest.main(verbosity=2)
