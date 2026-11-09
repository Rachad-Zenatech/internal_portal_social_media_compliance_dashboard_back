import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch
from uuid import uuid4

from services.compliance import submission_service as subs

SVC = "services.compliance.submission_service"


class SocialPostMediaTests(unittest.IsolatedAsyncioTestCase):
    def test_a_social_post_takes_images_not_video(self):
        allowed = subs.MAIN_EXT_BY_TYPE["SOCIAL_POST"]
        self.assertIn(".png", allowed)
        self.assertTrue(allowed.isdisjoint(subs.VIDEO_EXT))

    def test_other_types_are_unchanged(self):
        self.assertEqual(subs.MAIN_EXT_BY_TYPE["VIDEO"], subs.VIDEO_EXT)
        self.assertTrue(subs.VIDEO_EXT <= subs.MAIN_EXT_BY_TYPE["AD"])

    async def missing(self, content_type, has_video):
        row = {
            "id": uuid4(), "title": "Post", "brand_code": "ZENATECH", "audience": "GENERAL", "region": "Global",
            "platforms": ["FACEBOOK"], "account_ids": [uuid4()], "rule_sets": ["ZenaTech General"],
            "approval_needed_by": datetime.now(timezone.utc) + timedelta(days=2), "reviewer_ids": [uuid4()],
            "content_type": content_type, "caption_text": "A caption.", "script_required": False,
        }

        async def fetch_one(sql, *args):
            # The video query asks for video files; the "has a main file" query doesn't.
            return {"ok": 1} if ("video/%" not in sql or has_video) else None

        with patch(f"{SVC}.fetch_one", new=AsyncMock(side_effect=fetch_one)):
            return await subs._missing_for_submit(row)

    async def test_a_social_post_with_a_video_cant_be_sent(self):
        self.assertIn("main_file", await self.missing("SOCIAL_POST", has_video=True))

    async def test_a_social_post_with_an_image_or_none_can(self):
        self.assertNotIn("main_file", await self.missing("SOCIAL_POST", has_video=False))

    async def test_a_paid_ad_with_a_video_is_still_fine(self):
        self.assertNotIn("main_file", await self.missing("AD", has_video=True))


if __name__ == "__main__":
    unittest.main()
