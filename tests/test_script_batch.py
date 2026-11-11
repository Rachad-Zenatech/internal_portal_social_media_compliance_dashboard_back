import unittest
from datetime import datetime, timedelta, timezone
from uuid import uuid4

from fastapi import HTTPException

from services.compliance.evaluation import script_batch_service as svc


class DetailsTests(unittest.TestCase):
    def test_every_shared_detail_is_needed_before_sending(self):
        self.assertEqual(len(svc.missing_details({})), 8)

    def test_a_filled_in_batch_is_ready(self):
        details = {"brand_codes": ["ZENADRONE"], "audiences": ["PUBLIC"], "region": "North America", "platforms": ["LINKEDIN"],
                   "account_ids": [str(uuid4())], "rule_sets": ["GENERAL"], "approval_needed_by": "2026-12-01T10:00:00Z",
                   "reviewer_ids": [str(uuid4())]}
        self.assertEqual(svc.missing_details(details), [])

    def test_an_empty_list_counts_as_missing(self):
        self.assertIn("Platform(s)", svc.missing_details({"platforms": []}))

    def test_the_scripts_are_checked_as_video_posts_with_the_batchs_details(self):
        reviewer = uuid4()
        row = svc.post_row({"details": {"brand_codes": ["ZENADRONE", "DAAS"], "audiences": ["PUBLIC"], "platforms": ["LINKEDIN"],
                                        "rule_sets": ["GENERAL"], "region": "X", "reviewer_ids": [str(reviewer)]}}, "Launch")
        self.assertEqual((row["title"], row["content_type"], row["brand_code"], row["audience"]),
                         ("Launch", "VIDEO", "ZENADRONE", "PUBLIC"))
        self.assertEqual((row["brand_codes"], row["platforms"], row["reviewer_ids"]), (["ZENADRONE", "DAAS"], ["LINKEDIN"], [reviewer]))


class ApprovalBlockTests(unittest.TestCase):
    def test_a_clean_script_can_be_approved(self):
        self.assertIsNone(svc.approval_block({"score": 95, "violations": []}))

    def test_the_minimum_itself_is_enough(self):
        self.assertIsNone(svc.approval_block({"score": svc.MIN_APPROVAL_SCORE, "violations": []}))

    def test_a_low_score_blocks(self):
        self.assertIn("score is 60/100", svc.approval_block({"score": 60, "violations": []}))

    def test_a_critical_flag_blocks(self):
        self.assertIn("critical", svc.approval_block({"score": 90, "violations": [{"traffic_light": "RED"}]}))

    def test_a_script_the_ai_could_not_read_blocks(self):
        self.assertIn("AI review couldn't run", svc.approval_block({"score": 100, "ai_unavailable": True}))

    def test_a_script_with_no_check_blocks(self):
        self.assertIn("no compliance check", svc.approval_block(None))


class RecheckTests(unittest.TestCase):
    now = datetime.now(timezone.utc)

    def test_a_script_with_no_check_is_checked(self):
        self.assertTrue(svc._needs_recheck(None, "v1", self.now))

    def test_a_check_from_an_older_rulebook_is_checked_again(self):
        self.assertTrue(svc._needs_recheck({"rulebook_version": "old"}, "v1", self.now))
        self.assertFalse(svc._needs_recheck({"rulebook_version": "v1"}, "v1", self.now))

    def test_an_ai_down_check_is_retried_but_not_on_every_page_load(self):
        down = {"ai_unavailable": True}
        self.assertFalse(svc._needs_recheck(down, "v1", self.now))
        self.assertTrue(svc._needs_recheck(down, "v1", self.now - timedelta(seconds=svc.AI_RECHECK_AFTER_SEC + 5)))


class UploadTests(unittest.IsolatedAsyncioTestCase):
    async def refused(self, name, data):
        with self.assertRaises(HTTPException) as caught:
            await svc.read_upload(name, data)
        return caught.exception

    async def test_a_text_file_is_read(self):
        self.assertEqual(await svc.read_upload("scripts.txt", b"Script 1: A\nHello"), "Script 1: A\nHello")

    async def test_other_file_types_are_refused(self):
        self.assertEqual((await self.refused("clip.mp4", b"x")).status_code, 422)

    async def test_an_empty_file_is_refused(self):
        self.assertIn("empty", (await self.refused("a.txt", b"")).detail)

    async def test_a_file_with_no_text_is_refused(self):
        self.assertIn("No text", (await self.refused("a.txt", b"   \n  ")).detail)

    async def test_a_very_large_file_is_refused(self):
        self.assertEqual((await self.refused("a.txt", b"x" * (svc.MAX_UPLOAD_BYTES + 1))).status_code, 413)

    async def test_an_old_word_file_says_what_to_do(self):
        self.assertIn(".docx", (await self.refused("a.doc", b"x")).detail)


if __name__ == "__main__":
    unittest.main()
