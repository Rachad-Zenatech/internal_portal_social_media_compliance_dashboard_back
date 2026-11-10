import unittest
from datetime import datetime, timezone
from unittest.mock import AsyncMock, patch
from uuid import uuid4

from fastapi import HTTPException

from services.compliance.submissions import duplicate_service, submission_service

ACC_LI, ACC_X = uuid4(), uuid4()


def post(**over):
    row = {
        "id": uuid4(), "reference_code": "SUB-1", "title": "Launch", "status": "IN_REVIEW",
        "caption_text": "Meet the  ZenaDrone 1000.", "brand_codes": ["ZENADRONE"], "account_ids": [ACC_LI],
        "audiences": ["GENERAL"], "region": "Global", "platforms": ["LINKEDIN"],
        "publish_at": datetime(2026, 12, 1, 15, tzinfo=timezone.utc), "submitted_at": None, "accounts_json": [],
    }
    row.update(over)
    return row


async def find(mine, earlier, same_files=True):
    hashes = {mine["id"]: ["a"], **{e["id"]: ["a"] if same_files else ["b"] for e in earlier}}
    with (
        patch.object(duplicate_service, "fetch_all", new=AsyncMock(return_value=earlier)),
        patch.object(duplicate_service, "_content_hashes", new=AsyncMock(return_value=hashes)),
        patch.object(duplicate_service, "_labels", new=AsyncMock(return_value={f: {} for f, _ in duplicate_service.DETAIL_FIELDS})),
    ):
        return await duplicate_service.find(mine)


class CompareTests(unittest.TestCase):
    def test_captions_compare_without_case_or_spacing(self):
        self.assertEqual(duplicate_service.normalize_caption("  Meet the\n ZenaDrone "), "meet the zenadrone")

    def test_every_dropdown_sharing_a_value_is_a_full_overlap(self):
        mine = duplicate_service.details_of(post(platforms=["LINKEDIN", "X"]))
        shared, new, overlaps = duplicate_service.compare_details(mine, duplicate_service.details_of(post()))
        self.assertTrue(overlaps)
        self.assertEqual((shared["platforms"], new["platforms"]), (["LINKEDIN"], ["X"]))

    def test_a_different_platform_is_not_an_overlap(self):
        _, new, overlaps = duplicate_service.compare_details(
            duplicate_service.details_of(post(platforms=["X"], account_ids=[ACC_X])),
            duplicate_service.details_of(post()),
        )
        self.assertFalse(overlaps)
        self.assertEqual(new["platforms"], ["X"])

    def test_publish_dates_compare_by_day(self):
        a = datetime(2026, 12, 1, 9, tzinfo=timezone.utc)
        self.assertTrue(duplicate_service.same_publish_date(a, a.replace(hour=20)))
        self.assertFalse(duplicate_service.same_publish_date(a, a.replace(day=2)))
        self.assertTrue(duplicate_service.same_publish_date(None, None))


class FindTests(unittest.IsolatedAsyncioTestCase):
    async def test_same_post_same_details_same_date_is_blocked(self):
        earlier = post()
        report = await find(post(status="DRAFT"), [earlier])
        self.assertTrue(report.blocked)
        self.assertEqual(report.blocking.submission_id, earlier["id"])

    async def test_same_post_other_date_is_a_repost_to_confirm(self):
        report = await find(post(status="DRAFT", publish_at=datetime(2026, 12, 9, tzinfo=timezone.utc)), [post()])
        self.assertFalse(report.blocked)
        self.assertIsNotNone(report.repost)

    async def test_same_post_for_another_platform_is_allowed_and_listed(self):
        report = await find(post(status="DRAFT", platforms=["X"], account_ids=[ACC_X]), [post()])
        self.assertFalse(report.blocked)
        self.assertIsNone(report.repost)
        self.assertEqual(len(report.matches), 1)

    async def test_different_files_are_a_different_post(self):
        report = await find(post(status="DRAFT"), [post()], same_files=False)
        self.assertEqual(report.matches, [])


class SubmitGuardTests(unittest.IsolatedAsyncioTestCase):
    async def guard(self, report, confirm=False):
        with patch.object(duplicate_service, "find", new=AsyncMock(return_value=report)):
            await submission_service._require_not_duplicate(post(status="DRAFT"), confirm)

    async def test_a_duplicate_cant_be_submitted_even_when_confirmed(self):
        report = await find(post(status="DRAFT"), [post()])
        with self.assertRaises(HTTPException) as e:
            await self.guard(report, confirm=True)
        self.assertEqual((e.exception.status_code, e.exception.detail["code"]), (409, "DUPLICATE_POST"))

    async def test_a_repost_needs_confirming(self):
        report = await find(post(status="DRAFT", publish_at=datetime(2026, 12, 9, tzinfo=timezone.utc)), [post()])
        with self.assertRaises(HTTPException) as e:
            await self.guard(report)
        self.assertEqual(e.exception.detail["code"], "REPOST_CONFIRM")
        await self.guard(report, confirm=True)  # confirmed: goes through


if __name__ == "__main__":
    unittest.main()
