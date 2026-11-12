import os
import unittest
from datetime import datetime, timezone
from unittest.mock import AsyncMock, patch
from uuid import uuid4

from fastapi import HTTPException

from services.compliance.evaluation import script_batch_service as svc
from services.compliance.evaluation import script_review_service as scripts


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
        row = svc.post_row({"brand_codes": ["ZENADRONE", "DAAS"], "audiences": ["PUBLIC"], "platforms": ["LINKEDIN"],
                            "rule_sets": ["GENERAL"], "region": "X", "reviewer_ids": [str(reviewer)]}, "Launch")
        self.assertEqual((row["title"], row["content_type"], row["brand_code"], row["audience"]),
                         ("Launch", "VIDEO", "ZENADRONE", "PUBLIC"))
        self.assertEqual((row["brand_codes"], row["platforms"], row["reviewer_ids"]), (["ZENADRONE", "DAAS"], ["LINKEDIN"], [reviewer]))


class PerScriptDetailsTests(unittest.TestCase):
    batch = {"per_script_details": False, "details": {"brand_codes": ["ZENADRONE"], "platforms": ["LINKEDIN"]}}

    def test_with_one_set_every_script_gets_the_batchs_details(self):
        item = {"details": {"brand_codes": ["DAAS"]}}
        self.assertEqual(svc.effective_details(self.batch, item), {"brand_codes": ["ZENADRONE"], "platforms": ["LINKEDIN"]})

    def test_with_details_per_script_each_script_gets_its_own(self):
        batch = {**self.batch, "per_script_details": True}
        self.assertEqual(svc.effective_details(batch, {"details": {"brand_codes": ["DAAS"]}}), {"brand_codes": ["DAAS"]})
        self.assertEqual(svc.effective_details(batch, {"details": '{"platforms": ["X"]}'}), {"platforms": ["X"]})

    def test_a_script_with_no_details_of_its_own_has_none(self):
        batch = {**self.batch, "per_script_details": True}
        self.assertEqual(svc.effective_details(batch, {"details": None}), {})
        self.assertEqual(len(svc.missing_details(svc.effective_details(batch, {"details": None}))), 8)

    def test_a_script_is_read_the_same_whichever_way_the_row_was_loaded(self):
        batch = {**self.batch, "per_script_details": True}
        self.assertEqual(svc.effective_details(batch, {"item_details": {"region": "R"}}), {"region": "R"})

    def test_the_batch_alone_when_there_is_no_script(self):
        batch = {**self.batch, "per_script_details": True}
        self.assertEqual(svc.effective_details(batch, None), batch["details"])


class StatusTests(unittest.TestCase):
    def test_a_script_is_a_draft_until_its_post_is_made(self):
        self.assertEqual(svc.item_status(False, None), "DRAFT")
        self.assertEqual(svc.item_status(False, "IN_REVIEW"), "DRAFT")

    def test_a_script_is_what_it_is_on_its_post(self):
        self.assertEqual(svc.item_status(True, "IN_REVIEW"), "IN_REVIEW")
        self.assertEqual(svc.item_status(True, "APPROVED"), "APPROVED")
        self.assertEqual(svc.item_status(True, "CHANGES_REQUESTED"), "CHANGES_REQUESTED")

    def test_edits_the_reviewer_proposed_wait_for_the_creator(self):
        self.assertEqual(svc.item_status(True, "EDITS_PROPOSED"), "CHANGES_REQUESTED")

    def test_a_post_whose_script_was_never_sent_is_still_a_draft(self):
        self.assertEqual(svc.item_status(True, None), "DRAFT")
        self.assertEqual(svc.item_status(True, "DRAFT"), "DRAFT")

    def test_a_batch_nobody_sent_is_a_draft(self):
        self.assertEqual(svc.batch_status("DRAFT", ["DRAFT", "DRAFT"], False), "DRAFT")
        self.assertEqual(svc.batch_status("DRAFT", [], True), "DRAFT")

    def test_a_batch_is_in_review_while_any_script_waits_for_a_reviewer(self):
        self.assertEqual(svc.batch_status("DRAFT", ["APPROVED", "IN_REVIEW", "CHANGES_REQUESTED"], True), "IN_REVIEW")

    def test_a_batch_is_with_the_creator_when_nothing_waits_for_a_reviewer(self):
        self.assertEqual(svc.batch_status("DRAFT", ["APPROVED", "CHANGES_REQUESTED"], True), "CHANGES_REQUESTED")
        # A script added after the batch was sent is the creator's to send.
        self.assertEqual(svc.batch_status("DRAFT", ["APPROVED", "DRAFT"], True), "CHANGES_REQUESTED")

    def test_a_batch_is_done_when_every_script_is_approved(self):
        self.assertEqual(svc.batch_status("DRAFT", ["APPROVED", "APPROVED"], True), "DONE")

    def test_a_batch_being_sent_shows_as_processing(self):
        self.assertEqual(svc.batch_status("PROCESSING", ["IN_REVIEW"], True), "PROCESSING")


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


class NobodyApprovesTheirOwnScripts(unittest.IsolatedAsyncioTestCase):
    """The creator of a script can't approve it, even as a super admin: another reviewer has to."""

    async def may_act(self, module, row, user, *, reviewer=True, super_admin=False):
        # The rule as everywhere but development (where ALLOW_SUPER_ADMIN_SELF_REVIEW may be on in .env).
        with patch.dict(os.environ, {"ALLOW_SUPER_ADMIN_SELF_REVIEW": ""}), \
             patch.object(module.subs, "_can_review", new=AsyncMock(return_value=reviewer)), \
             patch.object(module.subs, "_is_super_admin", new=AsyncMock(return_value=super_admin)):
            return await module._reviewer_may_act(row, user)

    async def test_a_single_scripts_creator_cannot_review_it_even_as_a_super_admin(self):
        me, other = uuid4(), uuid4()
        row = {"created_by": me, "sent_at": True, "details": {}}
        self.assertFalse(await self.may_act(scripts, row, me, super_admin=True))
        self.assertTrue(await self.may_act(scripts, row, other))

    async def test_a_batchs_creator_cannot_review_it_even_as_a_super_admin(self):
        me, other = uuid4(), uuid4()
        row = {"created_by": me, "sent_at": datetime.now(timezone.utc), "details": {}}
        self.assertFalse(await self.may_act(svc, row, me, super_admin=True))
        self.assertTrue(await self.may_act(svc, row, other))

    async def test_someone_who_is_not_a_reviewer_cannot_review(self):
        row = {"created_by": uuid4(), "sent_at": datetime.now(timezone.utc), "details": {}}
        self.assertFalse(await self.may_act(scripts, row, uuid4(), reviewer=False))


if __name__ == "__main__":
    unittest.main()
