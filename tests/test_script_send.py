import json
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

from services.compliance.evaluation import script_review_service as srs

SUB = uuid4()
USER = uuid4()
ROW = {"id": SUB, "reference_code": "SUB-1", "title": "Test", "script_status": "DRAFT"}


def processing(minutes_ago=0):
    started = (datetime.now(timezone.utc) - timedelta(minutes=minutes_ago)).isoformat()
    return {"status": "PROCESSING", "started_at": started}


class SendStateTests(unittest.TestCase):
    def test_nothing_in_progress(self):
        self.assertEqual(srs._send_state({}), {"status": None, "error": None})
        self.assertEqual(srs._send_state({"script_send": None})["status"], None)

    def test_processing_then_timed_out(self):
        self.assertEqual(srs._send_state({"script_send": processing(1)})["status"], "PROCESSING")
        state = srs._send_state({"script_send": json.dumps(processing(30))})
        self.assertEqual(state["status"], "FAILED")
        self.assertIn("took too long", state["error"])

    def test_a_resumed_send_counts_from_when_it_was_picked_up(self):
        data = {**processing(30), "claimed_at": datetime.now(timezone.utc).isoformat()}
        self.assertEqual(srs._send_state({"script_send": data})["status"], "PROCESSING")

    def test_failed_keeps_its_reason(self):
        state = srs._send_state({"script_send": {"status": "FAILED", "error": "Error reading the script: bad file"}})
        self.assertEqual(state, {"status": "FAILED", "error": "Error reading the script: bad file"})


class RequiredDetailsTests(unittest.IsolatedAsyncioTestCase):
    FULL = {"title": "Test", "brand_code": "ZENADRONE", "audience": "GENERAL", "region": "GLOBAL", "platforms": ["FACEBOOK"],
            "rule_sets": ["ZenaDrone Defense"], "approval_needed_by": datetime.now(timezone.utc), "reviewer_ids": [uuid4()]}

    def test_a_complete_post_has_nothing_missing(self):
        with patch.object(srs.subs, "_account_ids", return_value=[uuid4()]):
            self.assertEqual(srs._missing_details(dict(self.FULL)), [])

    def test_empty_details_are_listed_by_their_form_names(self):
        row = {**self.FULL, "title": " ", "brand_code": None, "platforms": [], "approval_needed_by": None, "reviewer_ids": []}
        with patch.object(srs.subs, "_account_ids", return_value=[]):
            self.assertEqual(
                srs._missing_details(row),
                ["Title", "Brand", "Platform(s)", "Publishing account(s)", "Need approval by", "Send to reviewers"],
            )

    async def test_the_script_is_refused_until_they_are_filled_in(self):
        from fastapi import HTTPException

        row = {**self.FULL, "brand_code": None, "script_status": "DRAFT", "script_send": None, "script_required": True, "content_type": "VIDEO"}
        with patch.object(srs.subs, "_get_owned_row", new=AsyncMock(return_value=row)),              patch.object(srs.subs, "_require_editable"), patch.object(srs.subs, "_account_ids", return_value=[uuid4()]):
            with self.assertRaises(HTTPException) as err:
                await srs.submit(SUB, USER)
        self.assertEqual(err.exception.status_code, 422)
        self.assertIn("Brand", err.exception.detail)


class FailSendTests(unittest.IsolatedAsyncioTestCase):
    async def test_a_send_that_already_finished_is_not_marked_failed_or_announced(self):
        with patch.object(srs, "fetch_one", new=AsyncMock(return_value=None)),              patch.object(srs.subs, "_notify", new=AsyncMock()) as notify:
            await srs._fail_send(SUB, USER, "sending the script", "boom")
        notify.assert_not_awaited()

    async def test_a_running_send_that_fails_tells_the_creator(self):
        with patch.object(srs, "fetch_one", new=AsyncMock(return_value={"reference_code": "SUB-1", "title": "Test"})),              patch.object(srs.subs, "_notify", new=AsyncMock()) as notify:
            await srs._fail_send(SUB, USER, "reading the script", "bad file")
        self.assertIn("Error reading the script: bad file", notify.await_args.args[2])


class ProcessSendTests(unittest.IsolatedAsyncioTestCase):
    def patches(self, file, check=None, send_error=None):
        return [
            patch.object(srs.subs, "_get_owned_row", new=AsyncMock(return_value=ROW)),
            patch.object(srs, "_script_file", new=AsyncMock(return_value=file)),
            patch.object(srs, "_versions", new=AsyncMock(return_value=[])),
            patch.object(srs, "check_text", new=AsyncMock(return_value=check or {"score": 90}, side_effect=send_error)),
            patch.object(srs, "_send_checked", new=AsyncMock()),
            patch.object(srs, "_fail_send", new=AsyncMock()),
            patch.object(srs, "_require_not_duplicate", new=AsyncMock()),
        ]

    async def run_with(self, file, **kw):
        patchers = self.patches(file, **kw)
        mocks = [p.start() for p in patchers]
        try:
            await srs._process_send(SUB, USER)
        finally:
            for p in patchers:
                p.stop()
        return {"check": mocks[3], "send": mocks[4], "fail": mocks[5]}

    async def test_a_readable_script_is_scored_then_sent(self):
        m = await self.run_with({"id": uuid4(), "doc_text_status": "COMPLETED", "doc_text": "Hello world."})
        m["check"].assert_awaited_once()
        m["send"].assert_awaited_once()
        m["fail"].assert_not_awaited()

    async def test_an_unreadable_script_says_it_couldnt_be_read(self):
        m = await self.run_with({"id": uuid4(), "doc_text_status": "FAILED", "doc_text": None, "doc_text_error": "The PDF is empty"})
        step, detail = m["fail"].await_args.args[2], m["fail"].await_args.args[3]
        self.assertEqual(step, "reading the script")
        self.assertIn("PDF is empty", detail)
        m["send"].assert_not_awaited()

    async def test_a_scoring_error_says_the_score_couldnt_be_calculated(self):
        m = await self.run_with({"id": uuid4(), "doc_text_status": "COMPLETED", "doc_text": "Hello."}, send_error=RuntimeError("boom"))
        self.assertEqual(m["fail"].await_args.args[2], "calculating the compliance score")
        self.assertEqual(m["fail"].await_args.args[3], "boom")
        m["send"].assert_not_awaited()

    async def test_a_missing_file_fails_instead_of_hanging(self):
        m = await self.run_with(None)
        self.assertEqual(m["fail"].await_args.args[2], "reading the script")


if __name__ == "__main__":
    unittest.main()


class ScriptSentNoticeTests(unittest.IsolatedAsyncioTestCase):
    async def send(self, reviewers):
        conn = MagicMock(execute=AsyncMock())
        conn.transaction.return_value = MagicMock(__aenter__=AsyncMock(), __aexit__=AsyncMock(return_value=False))
        pool = MagicMock()
        pool.acquire.return_value = MagicMock(__aenter__=AsyncMock(return_value=conn), __aexit__=AsyncMock(return_value=False))
        row = {"id": SUB, "reference_code": "SUB-1040", "title": "Launch video", "created_by": USER}
        with (
            patch.object(srs, "get_pool", return_value=pool),
            patch.object(srs, "_lock_status", new=AsyncMock()),
            patch.object(srs, "_add_version", new=AsyncMock(return_value=2)),
            patch.object(srs.subs, "_add_event", new=AsyncMock()),
            patch.object(srs, "log_audit_event", new=AsyncMock()),
            patch.object(srs, "_send_to_reviewers", new=AsyncMock(return_value=reviewers)) as to_reviewers,
            patch.object(srs.subs, "_notify", new=AsyncMock()) as notify,
            patch.object(srs, "get_stage", new=AsyncMock(return_value="stage")),
        ):
            await srs._send_checked(row, "Script text", "UPLOAD", USER, {"score": 88})
        return to_reviewers, notify

    async def test_the_reviewers_and_the_creator_are_both_told(self):
        picked = [uuid4()]
        to_reviewers, notify = await self.send(picked)

        to_reviewers.assert_awaited_once()
        recipients, title, message = notify.await_args.args[:3]
        self.assertEqual(recipients, [USER])
        self.assertEqual(title, "Script sent for review: SUB-1040")
        self.assertIn("version 2 is with 1 reviewer: score 88/100", message)

    async def test_the_confirmation_counts_several_reviewers(self):
        _, notify = await self.send([uuid4(), uuid4()])
        self.assertIn("is with 2 reviewers", notify.await_args.args[2])
