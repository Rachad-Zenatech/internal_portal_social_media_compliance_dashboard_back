"""Duplicates are settled just before the compliance check (posts and scripts), not only when the post is sent."""
import unittest
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

from fastapi import HTTPException

from services.compliance.evaluation import script_review_service
from services.compliance.submissions import duplicate_service, submission_service
from tests.test_duplicate_posts import find, post

OTHER_DATE = datetime(2026, 12, 9, tzinfo=timezone.utc)


class PostCheckTests(unittest.IsolatedAsyncioTestCase):
    async def run_check(self, report, confirm=False):
        run = AsyncMock(side_effect=AssertionError("the compliance check ran"))
        with (
            patch.object(submission_service, "_get_owned_row", new=AsyncMock(return_value=post(status="DRAFT"))),
            patch.object(submission_service, "_check_text", new=MagicMock(return_value="Launch")),
            patch.object(submission_service, "require_details_complete", new=AsyncMock()),
            patch.object(duplicate_service, "find", new=AsyncMock(return_value=report)),
            patch.object(submission_service, "_run_check", new=run),
        ):
            with self.assertRaises(HTTPException) as e:
                await submission_service.run_initial_check(post()["id"], post()["id"], confirm_repost=confirm)
        return e.exception, run

    async def test_a_duplicate_is_refused_before_the_check_runs(self):
        exc, run = await self.run_check(await find(post(status="DRAFT"), [post()]))
        self.assertEqual((exc.status_code, exc.detail["code"]), (409, "DUPLICATE_POST"))
        run.assert_not_awaited()

    async def test_a_repost_is_confirmed_before_the_check_runs(self):
        exc, run = await self.run_check(await find(post(status="DRAFT", publish_at=OTHER_DATE), [post()]))
        self.assertEqual(exc.detail["code"], "REPOST_CONFIRM")
        run.assert_not_awaited()

    async def test_once_confirmed_the_compliance_check_runs(self):
        report = await find(post(status="DRAFT", publish_at=OTHER_DATE), [post()])
        with (
            patch.object(submission_service, "_get_owned_row", new=AsyncMock(return_value=post(status="DRAFT"))),
            patch.object(submission_service, "_check_text", new=MagicMock(return_value="Launch")),
            patch.object(submission_service, "require_details_complete", new=AsyncMock()),
            patch.object(duplicate_service, "find", new=AsyncMock(return_value=report)),
            patch.object(submission_service, "_run_check", new=AsyncMock(side_effect=RuntimeError("check reached"))),
        ):
            with self.assertRaisesRegex(RuntimeError, "check reached"):
                await submission_service.run_initial_check(post()["id"], post()["id"], confirm_repost=True)


class SendRemembersConfirmationTests(unittest.IsolatedAsyncioTestCase):
    async def test_a_repost_confirmed_at_the_check_isnt_asked_again(self):
        report = await find(post(status="DRAFT", publish_at=OTHER_DATE), [post()])
        with patch.object(duplicate_service, "find", new=AsyncMock(return_value=report)):
            await submission_service._require_not_duplicate(post(status="DRAFT"), False, report.repost.reference_code)

    async def test_a_different_earlier_post_is_asked_again(self):
        report = await find(post(status="DRAFT", publish_at=OTHER_DATE), [post()])
        with patch.object(duplicate_service, "find", new=AsyncMock(return_value=report)):
            with self.assertRaises(HTTPException) as e:
                await submission_service._require_not_duplicate(post(status="DRAFT"), False, "SUB-OTHER")
        self.assertEqual(e.exception.detail["code"], "REPOST_CONFIRM")


class ScriptPrecheckTests(unittest.IsolatedAsyncioTestCase):
    async def test_a_duplicate_script_is_refused_before_its_check_runs(self):
        report = await find(post(status="DRAFT"), [post()])
        check = AsyncMock(side_effect=AssertionError("the compliance check ran"))
        S = script_review_service
        with (
            patch.object(S.subs, "_get_owned_row", new=AsyncMock(return_value=post(status="DRAFT"))),
            patch.object(S.subs, "_require_editable", new=MagicMock()),
            patch.object(S, "_require_stage", new=MagicMock()),
            patch.object(S, "_status", new=MagicMock(return_value="DRAFT")),
            patch.object(S, "_read_uploaded", new=AsyncMock(return_value=({}, "the script"))),
            patch.object(duplicate_service, "find_script", new=AsyncMock(return_value=report)),
            patch.object(S, "check_text", new=check),
        ):
            with self.assertRaises(HTTPException) as e:
                await S.precheck(post()["id"], post()["id"])
        self.assertEqual((e.exception.status_code, e.exception.detail["code"]), (409, "DUPLICATE_SCRIPT"))
        check.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
