"""Execute the production INSERT against a temporary PostgreSQL table, never real submissions."""
import ast
import inspect
import os
import textwrap
import unittest
from uuid import uuid4

from postgresql_db.database import close_pool, create_pool
from services.compliance.submissions.submission_service import submit_for_review
from services.compliance.submissions import submission_service as service


@unittest.skipUnless(os.getenv("RUN_DATABASE_TESTS") == "1", "Requires the local PostgreSQL database")
class SubmissionReviewJobSqlTests(unittest.IsolatedAsyncioTestCase):
    async def test_all_submission_and_reviewer_statements_prepare(self):
        pool = await create_pool()
        checked = 0
        try:
            async with pool.acquire() as conn:
                for function in (service.submit_for_review, service.decide_review,
                                 service.reopen_approved, service.reschedule_approved):
                    tree = ast.parse(textwrap.dedent(inspect.getsource(function)))
                    for call in ast.walk(tree):
                        if not isinstance(call, ast.Call) or not call.args:
                            continue
                        sql = call.args[0]
                        if not isinstance(sql, ast.Constant) or not isinstance(sql.value, str):
                            continue
                        if not sql.value.lstrip().startswith(("SELECT ", "INSERT ", "UPDATE ")):
                            continue
                        with self.subTest(function=function.__name__, sql=sql.value.strip()[:100]):
                            await conn.prepare(sql.value)
                            checked += 1
            self.assertGreaterEqual(checked, 10)
        finally:
            await close_pool()

    async def test_review_and_authorized_publish_insert_without_parameter_conflicts(self):
        tree = ast.parse(textwrap.dedent(inspect.getsource(submit_for_review)))
        queries = [node.value for node in ast.walk(tree)
                   if isinstance(node, ast.Constant) and isinstance(node.value, str)
                   and "INSERT INTO content_review_jobs" in node.value]
        self.assertEqual(len(queries), 1)
        pool = await create_pool()
        try:
            async with pool.acquire() as conn:
                transaction = conn.transaction()
                await transaction.start()
                try:
                    await conn.execute("CREATE TEMP TABLE content_review_jobs "
                                       "(LIKE public.content_review_jobs INCLUDING DEFAULTS) ON COMMIT DROP")
                    for publish in (False, True):
                        with self.subTest(publish_after_changes=publish):
                            user_id = uuid4()
                            row = await conn.fetchrow(
                                queries[0], uuid4(), 1, "APPROVED" if publish else "AWAITING_REVIEW",
                                ["Compliance Review"], "{}", "GREEN", 100, user_id,
                                "APPROVE" if publish else None, "Revision approved" if publish else None,
                            )
                            self.assertEqual(row["status"], "APPROVED" if publish else "AWAITING_REVIEW")
                            self.assertEqual(row["decision"], "APPROVE" if publish else None)
                            self.assertEqual(row["decided_at"] is not None, publish)
                            self.assertEqual(row["completed_at"] is not None, publish)
                finally:
                    await transaction.rollback()
        finally:
            await close_pool()
