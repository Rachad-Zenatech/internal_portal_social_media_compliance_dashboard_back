"""Offline deadline checks plus isolated PostgreSQL handoff/concurrency regressions.

Set HANDOFF_TEST_DATABASE_URL to a disposable database named handoff_test. No AI,
email or external notification calls are made. The named test tables are reset.
"""
import asyncio
from contextlib import ExitStack
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, MagicMock, patch
from urllib.parse import urlparse
from uuid import uuid4

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import asyncpg
from fastapi import HTTPException
from services.compliance import submissions as package
from services.compliance.submissions import post_approval_suggestions as suggestions
from services.compliance.submissions import review_handoff as handoff
from services.compliance.submissions import review_handoff_release as release
from services.notifications.review_email import repository, policy

TEST_URL = os.getenv("HANDOFF_TEST_DATABASE_URL")
SCHEMA = """
CREATE TABLE IF NOT EXISTS content_submissions (
 id uuid PRIMARY KEY, status text, current_version integer, reference_code text, title text,
 created_by uuid, approval_needed_by timestamptz, publish_at timestamptz, publish_after_changes boolean,
 updated_by uuid, updated_at timestamptz, due_at timestamptz, due_kind text,
 due_reminded_at timestamptz, due_overdue_notified_at timestamptz, deleted_at timestamptz);
CREATE TABLE IF NOT EXISTS content_review_jobs (
 id bigserial PRIMARY KEY, submission_id uuid, version_number integer, status text,
 decision text, decided_by uuid, decided_at timestamptz);
CREATE TABLE IF NOT EXISTS content_submission_events (
 id bigserial PRIMARY KEY, submission_id uuid, event_type text, message text, data jsonb,
 actor_user_id uuid, created_at timestamptz DEFAULT NOW());
CREATE TABLE IF NOT EXISTS notifications (
 id bigserial PRIMARY KEY, user_id text, type text, title text, message text, link_url text,
 entity_type text, entity_id text, is_read boolean, created_at timestamptz);
CREATE TABLE IF NOT EXISTS review_email_state (activated_at timestamptz);
CREATE TABLE IF NOT EXISTS review_email_outbox (dedupe_key text);
CREATE TABLE IF NOT EXISTS script_documents (id uuid, created_by uuid, title text, decision_note text, deleted_at timestamptz);
CREATE TABLE IF NOT EXISTS script_document_events (id bigserial, document_id uuid, kind text, detail jsonb, user_id uuid, created_at timestamptz);
TRUNCATE content_submissions, content_review_jobs, content_submission_events, notifications,
 review_email_state, review_email_outbox, script_documents, script_document_events;
INSERT INTO review_email_state VALUES (NOW() - INTERVAL '1 day');
"""


def request(decision="REQUEST_CHANGES", publish_after_changes=False):
    return SimpleNamespace(decision=decision, publish_after_changes=publish_after_changes,
                           revision_days=2, revision_due_at=None, publish_now=False)


class DeadlineTests(unittest.TestCase):
    def test_exact_deadline_and_missing_deadline(self):
        now = datetime.now(timezone.utc)
        self.assertTrue(handoff.should_hold({"approval_needed_by": now + timedelta(seconds=1)}, now))
        self.assertFalse(handoff.should_hold({"approval_needed_by": now}, now))
        self.assertFalse(handoff.should_hold({"approval_needed_by": None}, now))

    def test_changes_deadline_cannot_precede_creator_handoff(self):
        until = datetime.now(timezone.utc) + timedelta(days=2)
        data = request()
        data.revision_due_at = until - timedelta(hours=1)
        with self.assertRaises(HTTPException) as error:
            handoff.revision_deadline({"approval_needed_by": until}, data, None)
        self.assertEqual(error.exception.status_code, 422)
        data.revision_due_at = None
        self.assertGreater(handoff.revision_deadline({"approval_needed_by": until}, data, None), until)


@unittest.skipUnless(TEST_URL, "Set HANDOFF_TEST_DATABASE_URL for isolated database regressions")
class DatabaseTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        if urlparse(TEST_URL).path != "/handoff_test":
            raise RuntimeError("Refusing to reset any database other than handoff_test")
        self.pool = await asyncpg.create_pool(TEST_URL, min_size=1, max_size=3, command_timeout=10)
        self.addAsyncCleanup(self.pool.close)
        await self.pool.execute(SCHEMA)
        self.creator, self.reviewer = uuid4(), uuid4()
        self.subs = SimpleNamespace(_add_event=self.event, _notify=AsyncMock(), _can_suggest=AsyncMock(return_value=True),
                                    _can_review=AsyncMock(return_value=True), _is_super_admin=AsyncMock(return_value=False))
        stack = ExitStack()
        self.addCleanup(stack.close)
        for target, name, value in ((handoff, "fetch_all", self.pool.fetch), (handoff, "fetch_one", self.pool.fetchrow),
                                    (release, "get_pool", lambda: self.pool), (package, "submission_service", self.subs),
                                    (package, "review_memory_service", SimpleNamespace(remember_soon=MagicMock()))):
            stack.enter_context(patch.object(target, name, value, create=True))

    async def event(self, conn, submission_id, kind, message, actor, data):
        await conn.execute("INSERT INTO content_submission_events (submission_id,event_type,message,actor_user_id,data) "
                           "VALUES ($1,$2,$3,$4,$5::jsonb)", submission_id, kind, message, actor, json.dumps(data))

    async def post(self, until=None):
        identity = uuid4()
        await self.pool.execute("""INSERT INTO content_submissions
            (id,status,current_version,reference_code,title,created_by,approval_needed_by,publish_after_changes)
            VALUES ($1,'IN_REVIEW',1,'SUB-TEST','Deadline test',$2,$3,false)""",
            identity, self.creator, until or datetime.now(timezone.utc) + timedelta(hours=1))
        await self.pool.execute("INSERT INTO content_review_jobs (submission_id,version_number,status) VALUES ($1,1,'AWAITING_REVIEW')", identity)
        return identity

    async def save(self, identity, data, actor=None):
        actor = actor or self.reviewer
        async with self.pool.acquire() as conn:
            async with conn.transaction():
                row = await conn.fetchrow("SELECT * FROM content_submissions WHERE id=$1 FOR UPDATE", identity)
                await handoff.require_undecided(conn, row)
                final = "APPROVED" if data.decision == "APPROVE" else "CHANGES_REQUESTED"
                await conn.execute("UPDATE content_review_jobs SET status=$2,decision=$3,decided_by=$4,decided_at=NOW() WHERE submission_id=$1",
                                   identity, final, data.decision, actor)
                due, held = await handoff.save_outcome(conn, row, data, actor, final, None)
                await handoff.record_outcome(conn, row, data, actor, "Please check the comments", None, due, "Decided", 95, [], held)
                return due, held

    async def expire(self, identity):
        await self.pool.execute("UPDATE content_submissions SET approval_needed_by=NOW()-INTERVAL '1 second' WHERE id=$1", identity)

    async def test_all_three_outcomes_stay_held_then_release_once(self):
        cases = (request("APPROVE"), request(publish_after_changes=True), request())
        for number, data in enumerate(cases, 1):
            with self.subTest(decision=data.decision, publish_after_changes=data.publish_after_changes):
                identity = await self.post()
                due, held = await self.save(identity, data)
                self.assertTrue(held)
                row = await self.pool.fetchrow("SELECT * FROM content_submissions WHERE id=$1", identity)
                self.assertEqual(row["status"], "IN_REVIEW")
                self.assertEqual(row["publish_after_changes"], data.publish_after_changes)
                self.assertFalse(suggestions.publishing_access(row).can_publish)
                self.assertEqual(await release.release_due(), 0)
                self.assertFalse([item for item in await self.pool.fetch(repository.OUTCOMES) if item["entity_id"] == str(identity)])
                await self.expire(identity)
                self.assertEqual(await release.release_due(), 1)
                final = await self.pool.fetchrow("SELECT * FROM content_submissions WHERE id=$1", identity)
                self.assertEqual(final["status"], "APPROVED" if data.decision == "APPROVE" else "CHANGES_REQUESTED")
                self.assertEqual(final["publish_after_changes"], data.publish_after_changes)
                self.assertEqual(final["due_at"], due)
                self.assertEqual(await release.release_due(), 0)
                self.assertEqual(await self.pool.fetchval("SELECT count(*) FROM notifications"), number)
                emails = [dict(item) for item in await self.pool.fetch(repository.OUTCOMES) if item["entity_id"] == str(identity)]
                self.assertEqual(len(emails), 1)
                self.assertEqual(emails[0]["event_kind"], "REVIEW_HANDOFF_RELEASED")
                self.assertEqual(policy.outcome_label(emails[0]), "Post approved" if data.decision == "APPROVE"
                                 else "Make changes, then publish" if data.publish_after_changes else "Changes requested")

    async def test_concurrent_reviewers_cannot_replace_the_first_decision(self):
        identity = await self.post()
        results = await asyncio.gather(self.save(identity, request("APPROVE")),
                                       self.save(identity, request(), uuid4()), return_exceptions=True)
        errors = [item for item in results if isinstance(item, HTTPException)]
        self.assertEqual(len(errors), 1)
        self.assertEqual(errors[0].status_code, 409)
        self.assertEqual(await self.pool.fetchval("SELECT count(*) FROM content_submission_events"), 1)

    async def test_failed_handoff_rolls_back_and_can_retry(self):
        identity = await self.post()
        await self.save(identity, request())
        await self.expire(identity)
        original = release.release_one

        async def fail_after_notice(*args):
            await original(*args)
            raise RuntimeError("Simulated worker failure")

        with patch.object(release, "release_one", fail_after_notice):
            with self.assertRaisesRegex(RuntimeError, "Simulated"):
                await release.release_due()
        self.assertEqual(await self.pool.fetchval("SELECT status FROM content_submissions WHERE id=$1", identity), "IN_REVIEW")
        self.assertEqual(await self.pool.fetchval("SELECT count(*) FROM notifications"), 0)
        self.assertEqual(await release.release_due(), 1)
        self.assertEqual(await self.pool.fetchval("SELECT count(*) FROM notifications"), 1)

    async def test_only_comments_remain_and_they_close_at_the_deadline(self):
        identity = await self.post()
        await self.save(identity, request(publish_after_changes=True))
        row = await self.pool.fetchrow("SELECT * FROM content_submissions WHERE id=$1", identity)
        view = SimpleNamespace(can_review=True, can_approve=True, can_delegate=True, can_manage_approval=False)
        await suggestions.enrich([view], [row], uuid4())
        self.assertTrue(view.review_decision_pending)
        self.assertTrue(view.can_suggest_after_approval)
        self.assertFalse(view.can_review or view.can_approve or view.can_delegate)
        await suggestions.require_comment(row, uuid4())
        await self.expire(identity)
        row = await self.pool.fetchrow("SELECT * FROM content_submissions WHERE id=$1", identity)
        with self.assertRaises(HTTPException) as error:
            await suggestions.require_comment(row, uuid4())
        self.assertEqual(error.exception.status_code, 403)
        await release.release_due()
        row = await self.pool.fetchrow("SELECT * FROM content_submissions WHERE id=$1", identity)
        with self.assertRaises(HTTPException):
            await suggestions.require_comment(row, uuid4())
        await suggestions.require_comment(row, self.creator)
        await suggestions.require_comment(row, self.reviewer)

    async def test_past_deadlines_keep_immediate_handoff_behavior(self):
        for data in (request("APPROVE"), request(publish_after_changes=True), request()):
            with self.subTest(decision=data.decision, publish_after_changes=data.publish_after_changes):
                identity = await self.post(datetime.now(timezone.utc) - timedelta(seconds=1))
                _, held = await self.save(identity, data)
                self.assertFalse(held)
                expected = "APPROVED" if data.decision == "APPROVE" else "CHANGES_REQUESTED"
                self.assertEqual(await self.pool.fetchval("SELECT status FROM content_submissions WHERE id=$1", identity), expected)
                self.assertEqual(await release.release_due(), 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
