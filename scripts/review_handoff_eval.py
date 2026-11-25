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
from typing import get_args
import unittest
from unittest.mock import AsyncMock, MagicMock, patch
from urllib.parse import urlparse
from uuid import uuid4

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import asyncpg
from fastapi import HTTPException
from models.compliance.submission_input_model import ContentType
from models.compliance.submission_model import ReviewCommentCreate
from services.compliance import submissions as package
from services.compliance.submissions import post_approval_suggestions as suggestions
from services.compliance.submissions import review_handoff as handoff
from services.compliance.submissions import review_handoff_release as release
from services.compliance.submissions import post_text_review as text_review, caption_review_service as caption_review
from services.compliance.submissions import review_comment_writes as comment_writes
from services.notifications.review_email import repository, policy

TEST_URL = os.getenv("HANDOFF_TEST_DATABASE_URL")
SCHEMA = """
CREATE TABLE IF NOT EXISTS content_submissions (
 id uuid PRIMARY KEY, content_type text, status text, current_version integer, reference_code text, title text,
 created_by uuid, approval_needed_by timestamptz, publish_at timestamptz, publish_after_changes boolean,
 updated_by uuid, updated_at timestamptz, due_at timestamptz, due_kind text,
 due_reminded_at timestamptz, due_overdue_notified_at timestamptz, deleted_at timestamptz);
ALTER TABLE content_submissions ADD COLUMN IF NOT EXISTS caption_text text;
ALTER TABLE content_submissions ADD COLUMN IF NOT EXISTS caption_html text;
ALTER TABLE content_submissions ADD COLUMN IF NOT EXISTS content_edited_at timestamptz;
ALTER TABLE content_submissions ADD COLUMN IF NOT EXISTS latest_check jsonb;
ALTER TABLE content_submissions ADD COLUMN IF NOT EXISTS platforms text[];
ALTER TABLE content_submissions ADD COLUMN IF NOT EXISTS platform_captions jsonb;
CREATE TABLE IF NOT EXISTS users (id uuid PRIMARY KEY, full_name text);
CREATE TABLE IF NOT EXISTS content_review_comments (
 id bigserial PRIMARY KEY, submission_id uuid, author_id uuid, body text, file_id uuid,
 region_x double precision, region_y double precision, region_w double precision, region_h double precision);
CREATE TABLE IF NOT EXISTS content_review_jobs (
 id bigserial PRIMARY KEY, submission_id uuid, version_number integer, status text,
 decision text, decided_by uuid, decided_at timestamptz);
ALTER TABLE content_review_jobs ADD COLUMN IF NOT EXISTS completed_at timestamptz;
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
 review_email_state, review_email_outbox, script_documents, script_document_events, users, content_review_comments;
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
                                    _can_review=AsyncMock(return_value=True), _is_super_admin=AsyncMock(return_value=False),
                                    _get_owned_row=self.owned, _platform_captions=lambda row: json.loads(row.get('platform_captions') or '{}'),
                                    _json=lambda value: json.loads(value) if isinstance(value, str) else value)
        stack = ExitStack()
        self.addCleanup(stack.close)
        for target, name, value in ((handoff, "fetch_all", self.pool.fetch), (handoff, "fetch_one", self.pool.fetchrow),
                                    (text_review, "get_pool", lambda: self.pool), (caption_review, "get_pool", lambda: self.pool),
                                    (comment_writes, "get_pool", lambda: self.pool),
                                    (caption_review, "fetch_all", self.pool.fetch),
                                    (release, "get_pool", lambda: self.pool), (package, "submission_service", self.subs),
                                    (package, "review_memory_service", SimpleNamespace(remember_soon=MagicMock()))):
            stack.enter_context(patch.object(target, name, value, create=True))

    async def owned(self, submission_id, user_id, **kwargs):
        return dict(await self.pool.fetchrow('SELECT * FROM content_submissions WHERE id=$1', submission_id))

    async def event(self, conn, submission_id, kind, message, actor, data):
        await conn.execute("INSERT INTO content_submission_events (submission_id,event_type,message,actor_user_id,data) "
                           "VALUES ($1,$2,$3,$4,$5::jsonb)", submission_id, kind, message, actor, json.dumps(data))

    async def post(self, until=None, content_type="TEXT_POST"):
        identity = uuid4()
        await self.pool.execute("""INSERT INTO content_submissions
            (id,status,current_version,reference_code,title,created_by,approval_needed_by,publish_after_changes,content_type)
            VALUES ($1,'IN_REVIEW',1,'SUB-TEST','Deadline test',$2,$3,false,$4)""",
            identity, self.creator, until or datetime.now(timezone.utc) + timedelta(hours=1), content_type)
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
        for number, (content_type, data) in enumerate(((kind, outcome) for kind in get_args(ContentType) for outcome in cases), 1):
            with self.subTest(content_type=content_type, decision=data.decision, publish_after_changes=data.publish_after_changes):
                identity = await self.post(content_type=content_type)
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

    async def test_earlier_pending_decisions_keep_their_original_outcome(self):
        for data in (request("APPROVE"), request(publish_after_changes=True), request()):
            with self.subTest(decision=data.decision, publish_after_changes=data.publish_after_changes):
                identity = await self.post()
                await self.pool.execute("""UPDATE content_review_jobs SET decision=$2,decided_by=$3,
                    decided_at=NOW() WHERE submission_id=$1""", identity, data.decision, self.reviewer)
                async with self.pool.acquire() as conn:
                    await self.event(conn, identity, "REVIEW_DECISION_HELD", "Earlier recorded decision", self.reviewer,
                                     {"version": 1, "request": {**vars(data), "revision_due_at": None}})
                self.assertEqual(await release.release_due(), 0)
                await self.expire(identity)
                self.assertEqual(await release.release_due(), 1)
                expected = "APPROVED" if data.decision == "APPROVE" else "CHANGES_REQUESTED"
                row = await self.pool.fetchrow("SELECT * FROM content_submissions WHERE id=$1", identity)
                self.assertEqual(row["status"], expected)
                self.assertEqual(row["publish_after_changes"], data.publish_after_changes)
                job = await self.pool.fetchrow("SELECT * FROM content_review_jobs WHERE submission_id=$1", identity)
                self.assertEqual(job["status"], expected)
                self.assertIsNotNone(job["completed_at"])
                self.assertEqual(await release.release_due(), 0)

    async def test_text_suggestions_remain_editable_until_deadline_for_every_outcome_and_post_type(self):
        cases = (request('APPROVE'), request(publish_after_changes=True), request())
        for content_type, data in ((kind, outcome) for kind in get_args(ContentType) for outcome in cases):
            with self.subTest(content_type=content_type, decision=data.decision, publishing=data.publish_after_changes):
                identity, reviewer = await self.post(content_type=content_type), uuid4()
                await self.pool.execute("""UPDATE content_submissions SET caption_text='Original text',
                    content_edited_at=NOW()-INTERVAL '1 minute', latest_check='{"score":95}'::jsonb,
                    platforms=ARRAY['INSTAGRAM'] WHERE id=$1""", identity)
                await self.save(identity, data)
                original_job = await self.pool.fetchrow('SELECT * FROM content_review_jobs WHERE submission_id=$1', identity)
                if content_type == 'TEXT_POST':
                    get, save = text_review.get_review, text_review.save_review
                    edit = SimpleNamespace(text='Suggested text', base_text='Original text', base_revision=None)
                else:
                    get, save = caption_review.get_caption_review, caption_review.save_caption
                    edit = SimpleNamespace(caption_text='Suggested text', base_caption='Original text', platform=None)
                self.assertTrue((await get(identity, reviewer)).can_edit)
                self.assertFalse((await get(identity, self.creator)).can_edit)
                with self.assertRaises(HTTPException) as creator_error:
                    await save(identity, self.creator, edit)
                self.assertEqual(creator_error.exception.status_code, 403)
                with patch.object(self.subs, '_can_suggest', AsyncMock(return_value=False)):
                    self.assertFalse((await get(identity, reviewer)).can_edit)
                    with self.assertRaises(HTTPException) as permission_error:
                        await save(identity, reviewer, edit)
                    self.assertEqual(permission_error.exception.status_code, 403)
                await save(identity, reviewer, edit)
                row = await self.owned(identity, reviewer)
                self.assertEqual(row['caption_text'], 'Suggested text')
                self.assertEqual(row['status'], 'IN_REVIEW')
                self.assertEqual(await self.pool.fetchrow('SELECT * FROM content_review_jobs WHERE submission_id=$1', identity), original_job)
                self.assertTrue(row['latest_check'] is None or json.loads(row['latest_check'])['review_caption_edited'])
                self.assertEqual(await release.release_due(), 0)
                self.subs._notify.assert_not_awaited()
                with self.assertRaises(HTTPException):
                    await handoff.require_undecided(None, row)
                await self.expire(identity)
                self.assertFalse((await get(identity, reviewer)).can_edit)
                with self.assertRaises(HTTPException) as expired_error:
                    await save(identity, reviewer, edit)
                self.assertEqual(expired_error.exception.status_code, 403)
                self.assertEqual((await self.owned(identity, reviewer))['caption_text'], 'Suggested text')
                await release.release_due()

    async def test_platform_text_suggestions_use_the_same_deadline(self):
        cases = (request('APPROVE'), request(publish_after_changes=True), request())
        for kind, data in ((kind, data) for kind in ('SOCIAL_POST', 'IMAGE', 'CAROUSEL') for data in cases):
            with self.subTest(content_type=kind, decision=data.decision, publishing=data.publish_after_changes):
                identity, reviewer = await self.post(content_type=kind), uuid4()
                await self.pool.execute("UPDATE content_submissions SET caption_text='Main text', platforms=ARRAY['INSTAGRAM'] WHERE id=$1", identity)
                await self.save(identity, data)
                edit = SimpleNamespace(caption_text='Platform suggestion', base_caption='Main text', platform='INSTAGRAM')
                await caption_review.save_caption(identity, reviewer, edit)
                row = await self.owned(identity, reviewer)
                self.assertEqual(row['caption_text'], 'Main text')
                self.assertEqual(json.loads(row['platform_captions'])['INSTAGRAM'], 'Platform suggestion')
                await self.expire(identity)
                with self.assertRaises(HTTPException) as error:
                    await caption_review.save_caption(identity, reviewer, edit)
                self.assertEqual(error.exception.status_code, 403)
                await release.release_due()

    async def test_text_comments_and_image_region_suggestions_use_the_same_deadline(self):
        query = """INSERT INTO content_review_comments
            (submission_id,author_id,body,file_id,region_x,region_y,region_w,region_h)
            VALUES ($1,$2,$3,$4,$5,$6,$7,$8) RETURNING id"""
        cases = (request('APPROVE'), request(publish_after_changes=True), request())
        for kind, data in ((kind, data) for kind in get_args(ContentType) for data in cases):
            with self.subTest(content_type=kind, decision=data.decision, publishing=data.publish_after_changes):
                identity, reviewer = await self.post(content_type=kind), uuid4()
                await self.save(identity, data)
                original_job = await self.pool.fetchrow('SELECT * FROM content_review_jobs WHERE submission_id=$1', identity)
                comments = [ReviewCommentCreate(body='A text suggestion')]
                if kind in ('SOCIAL_POST', 'IMAGE', 'CAROUSEL'):
                    comments.append(ReviewCommentCreate(body='Change this part of the image', file_id=uuid4(),
                                                       region_x=0.1, region_y=0.1, region_w=0.2, region_h=0.2))
                for comment in comments:
                    args = (identity, reviewer, comment.body, comment.file_id,
                            comment.region_x, comment.region_y, comment.region_w, comment.region_h)
                    self.assertIsNotNone(await comment_writes.write(identity, reviewer, query, *args))
                self.assertEqual(await self.pool.fetchrow('SELECT * FROM content_review_jobs WHERE submission_id=$1', identity), original_job)
                await self.expire(identity)
                with self.assertRaises(HTTPException) as error:
                    await comment_writes.write(identity, reviewer, query, *args)
                self.assertEqual(error.exception.status_code, 403)
                self.assertEqual(await self.pool.fetchval('SELECT count(*) FROM content_review_comments WHERE submission_id=$1', identity), len(comments))
                await release.release_due()

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
