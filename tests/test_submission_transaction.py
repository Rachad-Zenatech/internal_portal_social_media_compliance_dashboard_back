"""Run the submission service's database writes on rollback-only temporary tables."""
import os
import unittest
from contextlib import ExitStack, asynccontextmanager
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from uuid import uuid4

from postgresql_db.database import close_pool, create_pool
from services.compliance.submissions import submission_service as service
from models.compliance.submission_model import ReviewDecisionRequest


@asynccontextmanager
async def borrow_connection(conn):
    yield conn


@unittest.skipUnless(os.getenv("RUN_DATABASE_TESTS") == "1", "Requires local PostgreSQL")
class SubmissionTransactionTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        pool = await create_pool()
        self.acquired = pool.acquire()
        self.conn = await self.acquired.__aenter__()
        self.transaction = self.conn.transaction()
        await self.transaction.start()
        for table in ("content_submissions", "content_submission_versions", "content_review_jobs"):
            await self.conn.execute(f"CREATE TEMP TABLE {table} "
                                    f"(LIKE public.{table} INCLUDING DEFAULTS) ON COMMIT DROP")
        await self.conn.execute("CREATE TEMP TABLE content_submission_events "
                                "(LIKE public.content_submission_events INCLUDING DEFAULTS) ON COMMIT DROP")
        await self.conn.execute("CREATE TEMP SEQUENCE test_submission_event_ids")
        await self.conn.execute("ALTER TABLE pg_temp.content_submission_events ALTER COLUMN id "
                                "SET DEFAULT nextval('pg_temp.test_submission_event_ids')")
        await self.conn.execute("CREATE TEMP TABLE creator_flag_dismissal_requests "
                                "(submission_id UUID, version_number INT, status VARCHAR(20)) ON COMMIT DROP")

    async def asyncTearDown(self):
        await self.transaction.rollback()
        await self.acquired.__aexit__(None, None, None)
        await close_pool()

    async def run_flow(self, status, permission):
        now = datetime.now(timezone.utc)
        user_id, reviewer_id, submission_id = uuid4(), uuid4(), uuid4()
        version = 0 if status == "DRAFT" else 1
        row = dict(id=submission_id, reference_code=f"TEST-{submission_id.hex[:8]}",
                   title="Transaction test", content_type="SOCIAL_POST", brand_code="ZENADRONE",
                   brand_codes=["ZENADRONE"], audience="PUBLIC", audiences=["PUBLIC"], region="Global",
                   platforms=["LINKEDIN"], account_ids=[], reviewer_ids=[reviewer_id],
                   reviewer_route=["Compliance Review"], rule_sets=["Rulebook"], caption_html="Caption",
                   caption_text="Caption", latest_check={}, created_by=user_id, current_version=version,
                   status=status, publish_after_changes=permission, publish_at=now + timedelta(days=2),
                   approval_needed_by=now + timedelta(days=1))
        await self.conn.execute(
            "INSERT INTO content_submissions "
            "(id, reference_code, title, content_type, status, current_version, publish_after_changes, publish_at) "
            "VALUES ($1, $2, $3, $4, $5, $6, $7, $8)",
            submission_id, row["reference_code"], row["title"], row["content_type"], status, version,
            permission, row["publish_at"],
        )
        result = SimpleNamespace(traffic_light="GREEN", score=100, violations_count=0)
        await self.conn.execute("INSERT INTO creator_flag_dismissal_requests VALUES ($1, $2, 'PENDING')",
                                submission_id, version)
        # All persistence and event SQL is real. External reads/notifications are isolated.
        mocks = {
            "_get_owned_row": AsyncMock(return_value=row),
            "_missing_for_submit": AsyncMock(return_value=[]),
            "_account_ids": lambda unused: [],
            "_accounts": lambda unused: [],
            "_connected_accounts": AsyncMock(return_value=[]),
            "_require_not_duplicate": AsyncMock(),
            "_creator_exclusion": AsyncMock(return_value=user_id),
            "_reviewer_ids": AsyncMock(return_value=[reviewer_id]),
            "_suggester_ids": AsyncMock(return_value=[]),
            "_approvers": lambda *unused: ([], []),
            "compliance_rulebook_version": AsyncMock(return_value=1),
            "_list_files": AsyncMock(return_value=[]),
            "fetch_one": AsyncMock(return_value=None),
            "approved_script": AsyncMock(return_value=None),
            "get_pool": lambda: SimpleNamespace(acquire=lambda: borrow_connection(self.conn)),
            "log_audit_event": AsyncMock(),
            "_notify_reviewers": AsyncMock(),
            "get_submission": AsyncMock(return_value="saved submission"),
        }
        with ExitStack() as patches:
            for name, replacement in mocks.items():
                patches.enter_context(patch.object(service, name, replacement))
            patches.enter_context(patch.object(service.approval_path_service, "load_rules", AsyncMock(return_value=[])))
            patches.enter_context(patch.object(service.manual_check_service, "require_current_check", return_value=(result, {})))
            patches.enter_context(patch("services.notifications.notification_service.create_notification", new=AsyncMock()))
            saved, job = await service.submit_for_review(submission_id, user_id)
        self.assertEqual(saved, "saved submission")
        published = status == "CHANGES_REQUESTED" and permission
        current = await self.conn.fetchrow("SELECT * FROM content_submissions WHERE id = $1", submission_id)
        self.assertEqual(current["status"], "APPROVED" if published else "IN_REVIEW")
        self.assertEqual(current["current_version"], version + 1)
        carried_version = await self.conn.fetchval(
            "SELECT version_number FROM creator_flag_dismissal_requests WHERE submission_id=$1", submission_id)
        self.assertEqual(carried_version, version + 1)
        self.assertFalse(current["publish_after_changes"])
        self.assertEqual(current["due_at"] is None, published)
        self.assertEqual(current["due_kind"], None if published else "REVIEW")
        self.assertEqual(job.decision, "APPROVE" if published else None)
        self.assertEqual(job.decided_at is not None, published)
        snapshots = await self.conn.fetchval("SELECT count(*) FROM content_submission_versions WHERE submission_id=$1", submission_id)
        self.assertEqual(snapshots, 1)
        events = await self.conn.fetch("SELECT event_type FROM content_submission_events WHERE submission_id=$1", submission_id)
        expected = {"SUBMITTED", "CHECK_COMPLETED"} | (set() if published else {"REVIEW_REQUESTED"})
        self.assertEqual({event["event_type"] for event in events}, expected)
        self.assertEqual(mocks["_notify_reviewers"].await_count, 0 if published else 1)

    async def test_new_submission(self):
        await self.run_flow("DRAFT", False)

    async def test_resubmit_for_review(self):
        await self.run_flow("CHANGES_REQUESTED", False)

    async def test_publish_after_changes(self):
        await self.run_flow("CHANGES_REQUESTED", True)

    async def test_draft_flag_does_not_bypass_review(self):
        await self.run_flow("DRAFT", True)

    async def run_reviewer_decision(self, decision, permission=False):
        submission_id, creator_id, reviewer_id = uuid4(), uuid4(), uuid4()
        row = dict(id=submission_id, reference_code=f"TEST-{submission_id.hex[:8]}", title="Review test",
                   created_by=creator_id, current_version=1, status="IN_REVIEW", latest_check={},
                   publish_at=datetime.now(timezone.utc) + timedelta(days=2))
        await self.conn.execute(
            "INSERT INTO content_submissions (id, reference_code, title, content_type, status, current_version) "
            "VALUES ($1, $2, $3, 'SOCIAL_POST', 'IN_REVIEW', 1)",
            submission_id, row["reference_code"], row["title"],
        )
        await self.conn.execute(
            "INSERT INTO content_review_jobs (submission_id, version_number, status, reviewer_route, created_by) "
            "VALUES ($1, 1, 'AWAITING_REVIEW', '{}', $2)", submission_id, creator_id,
        )

        async def read_metadata(sql, *args):
            return {"n": 1} if "count(*)" in sql else {"full_name": "Reviewer"}

        mocks = {
            "_get_owned_row": AsyncMock(return_value=row),
            "_can_review": AsyncMock(return_value=True),
            "_is_super_admin": AsyncMock(return_value=False),
            "fetch_one": AsyncMock(side_effect=read_metadata),
            "get_pool": lambda: SimpleNamespace(acquire=lambda: borrow_connection(self.conn)),
            "log_audit_event": AsyncMock(), "_notify": AsyncMock(),
            "get_submission": AsyncMock(return_value="saved decision"),
        }
        request = ReviewDecisionRequest(decision=decision, notes="Please fix the caption",
                                        publish_after_changes=permission, publish_now=decision == "APPROVE")
        with ExitStack() as patches:
            for name, replacement in mocks.items():
                patches.enter_context(patch.object(service, name, replacement))
            patches.enter_context(patch.object(service.review_approval_service, "require_approval", new=AsyncMock(return_value=(row, 100))))
            patches.enter_context(patch.object(service.review_memory_service, "remember_soon"))
            await service.decide_review(submission_id, reviewer_id, request)
        current = await self.conn.fetchrow("SELECT * FROM content_submissions WHERE id=$1", submission_id)
        changes = decision == "REQUEST_CHANGES"
        self.assertEqual(current["status"], "CHANGES_REQUESTED" if changes else "APPROVED")
        self.assertEqual(current["publish_after_changes"], permission)
        self.assertEqual(current["due_kind"], "REVISION" if changes else None)
        self.assertEqual(current["due_at"] is not None, changes)
        job = await self.conn.fetchrow("SELECT * FROM content_review_jobs WHERE submission_id=$1", submission_id)
        self.assertEqual(job["decision"], decision)
        self.assertEqual(job["decided_by"], reviewer_id)
        self.assertIsNotNone(job["decided_at"])

    async def test_reviewer_requests_changes_and_resubmission(self):
        await self.run_reviewer_decision("REQUEST_CHANGES")

    async def test_reviewer_allows_publish_after_changes(self):
        await self.run_reviewer_decision("REQUEST_CHANGES", True)

    async def test_reviewer_approves_content(self):
        await self.run_reviewer_decision("APPROVE")
