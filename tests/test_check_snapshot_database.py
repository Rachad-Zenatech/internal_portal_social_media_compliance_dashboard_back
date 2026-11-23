"""Integration checks in an isolated schema; opt in with a dedicated test DSN only."""

import json
import os
import unittest
from unittest.mock import patch
from uuid import UUID, uuid4

import asyncpg

from models.compliance.flag_review_model import FlagDismissRequest
from postgresql_db.schemas.check_snapshots_schema import ensure_check_snapshots
from postgresql_db.schemas.creator_flag_dismissal_requests_schema import ensure_creator_flag_dismissal_requests
from postgresql_db.schemas.submission_tables_schema import _create_flag_verdicts
from services.compliance.submissions import check_snapshot_feedback as feedback
from services.compliance.submissions import check_snapshot_service as snapshots
from tests.test_check_snapshot_capture import example


@unittest.skipUnless(os.getenv("COMPLIANCE_SNAPSHOT_TEST_DSN"), "Requires a dedicated snapshot test database")
class SnapshotDatabaseTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.schema = "snapshot_test_" + uuid4().hex
        self.dsn = os.environ["COMPLIANCE_SNAPSHOT_TEST_DSN"]
        self.admin = await asyncpg.connect(self.dsn)
        await self.admin.execute(f'CREATE SCHEMA "{self.schema}"')
        self.pool = await asyncpg.create_pool(self.dsn, min_size=1, max_size=3,
                                              server_settings={"search_path": self.schema})
        async with self.pool.acquire() as conn:
            await conn.execute("""CREATE TABLE users (id UUID PRIMARY KEY);
                CREATE TABLE content_submissions (id UUID PRIMARY KEY, current_version INT, latest_check JSONB);""")
            await _create_flag_verdicts(conn)
            await ensure_creator_flag_dismissal_requests(conn)
            await ensure_check_snapshots(conn)
            await ensure_check_snapshots(conn)
        self.pool_patch = patch.object(snapshots, "get_pool", return_value=self.pool)
        self.pool_patch.start()
        row, request, self.captured, self.original = example()
        self.post, self.evaluated = snapshots.inputs(row, request)
        self.post_id = UUID(self.post["id"])
        self.rule_id = UUID(self.original["violations"][0]["rule_id"])
        self.user_id = uuid4()
        async with self.pool.acquire() as conn:
            await conn.execute("INSERT INTO users VALUES ($1)", self.user_id)
            await conn.execute("INSERT INTO content_submissions VALUES ($1,1,NULL)", self.post_id)
        self.check = await self.save_check()

    async def asyncTearDown(self):
        self.pool_patch.stop()
        await self.pool.close()
        await self.admin.execute(f'DROP SCHEMA "{self.schema}" CASCADE')
        await self.admin.close()

    async def save_check(self):
        check = await snapshots.save(self.post, self.evaluated, self.captured, self.original)
        async with self.pool.acquire() as conn:
            await conn.execute("UPDATE content_submissions SET latest_check=$2::jsonb WHERE id=$1",
                               self.post_id, json.dumps(check))
        return check

    async def verdict(self, verdict, reason=None):
        async with self.pool.acquire() as conn:
            async with conn.transaction():
                return await feedback.save_verdict(conn, self.post_id, 1, self.rule_id,
                                                   self.check["violations"][0], verdict, reason, "Reviewer note", self.user_id)

    async def test_original_scores_content_rules_and_exact_links_survive_reruns(self):
        first = self.check
        await self.verdict("OVER_FLAGGED", "NEGATED")
        self.post["title"] = "Edited title"
        self.captured["rules"][str(self.rule_id)]["prohibited_terms"] = ["edited rule"]
        self.original["score"] = 100
        self.check = await self.save_check()
        async with self.pool.acquire() as conn:
            saved = await conn.fetchrow("""SELECT c.check_result, c.post_details, f.rule_snapshot
                FROM compliance_check_snapshots c JOIN compliance_finding_snapshots f ON f.check_snapshot_id=c.id
                JOIN compliance_feedback_events e ON e.finding_snapshot_id=f.id WHERE c.id=$1""",
                                       UUID(first["check_snapshot_id"]))
            self.assertEqual(json.loads(saved["check_result"])["score"], 75)
            self.assertEqual(json.loads(saved["post_details"])["title"], "Original")
            self.assertEqual(json.loads(saved["rule_snapshot"])["definition"]["prohibited_terms"], ["wording"])
            self.assertEqual(await conn.fetchval("SELECT count(*) FROM compliance_check_snapshots"), 2)
            self.assertNotEqual(first["check_snapshot_id"], self.check["check_snapshot_id"])

    async def test_pending_requests_are_not_labels_and_evidence_is_not_a_false_positive(self):
        finding = self.check["violations"][0]
        row = {"id": self.post_id, "current_version": 1}
        async with self.pool.acquire() as conn:
            await feedback.save_request(conn, row, self.rule_id, finding,
                                        FlagDismissRequest(reason="EVIDENCE_ON_FILE", note="Document 1"),
                                        "Document 1", self.user_id)
            self.assertEqual(await conn.fetchval("SELECT count(*) FROM compliance_feedback_events"), 0)
            self.assertEqual(await conn.fetchval("SELECT finding_snapshot_id FROM creator_flag_dismissal_requests"),
                             UUID(finding["finding_snapshot_id"]))
        await self.verdict("CORRECT", "EVIDENCE_ON_FILE")
        async with self.pool.acquire() as conn:
            self.assertEqual(await conn.fetchval("SELECT resolution FROM compliance_feedback_events"), "EVIDENCE_SUPPLIED")
            self.assertEqual(await conn.fetchval("SELECT latest_check->>'score' FROM content_submissions"), "75")

    async def test_undo_and_replacement_preserve_feedback_history(self):
        await self.verdict("CORRECT")
        async with self.pool.acquire() as conn:
            async with conn.transaction():
                await feedback.remove_verdict(conn, self.post_id, 1, self.rule_id, "CORRECT", self.user_id)
        await self.verdict("OVER_FLAGGED", "DIFFERENT_MEANING")
        async with self.pool.acquire() as conn:
            events = await conn.fetch("SELECT verdict FROM compliance_feedback_events ORDER BY decided_at")
            self.assertEqual([e["verdict"] for e in events], ["CORRECT", "RETRACTED", "OVER_FLAGGED"])

    async def test_history_is_append_only_and_survives_draft_deletion(self):
        await self.verdict("CORRECT")
        async with self.pool.acquire() as conn:
            for table in ("compliance_check_snapshots", "compliance_finding_snapshots", "compliance_feedback_events"):
                for statement in (f"UPDATE {table} SET id=id", f"DELETE FROM {table}"):
                    with self.assertRaises(asyncpg.PostgresError) as raised:
                        await conn.execute(statement)
                    self.assertEqual(raised.exception.sqlstate, "55000")
            await conn.execute("DELETE FROM content_submissions WHERE id=$1", self.post_id)
            self.assertEqual(await conn.fetchval("SELECT count(*) FROM compliance_check_snapshots"), 1)
            self.assertEqual(await conn.fetchval("SELECT count(*) FROM compliance_feedback_events"), 1)

    async def test_feedback_failure_rolls_back_and_old_check_cannot_be_labeled(self):
        with self.assertRaises(asyncpg.CheckViolationError):
            await self.verdict("INVALID")
        async with self.pool.acquire() as conn:
            self.assertEqual(await conn.fetchval("SELECT count(*) FROM finding_verdicts"), 0)
        await self.save_check()
        with self.assertRaises(Exception) as raised:
            await self.verdict("CORRECT")
        self.assertEqual(getattr(raised.exception, "status_code", None), 409)

    async def test_partial_snapshot_write_is_rolled_back(self):
        broken = {**self.original, "violations": [{"rule_id": "invalid-uuid"}]}
        with self.assertRaises(ValueError):
            await snapshots.save(self.post, self.evaluated, self.captured, broken)
        async with self.pool.acquire() as conn:
            self.assertEqual(await conn.fetchval("SELECT count(*) FROM compliance_check_snapshots"), 1)
