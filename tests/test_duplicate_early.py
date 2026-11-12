"""Duplicates are looked up as soon as the details are filled in, scripts included, not only when the post is sent."""

import unittest
from unittest.mock import AsyncMock, patch
from uuid import uuid4

from models.compliance.script_review_model import ScriptVersion
from models.compliance.submission_model import DuplicateReport
from services.compliance.evaluation import script_review_service as srs
from tools.compliance.submissions import submission_router as router

USER = uuid4()


def version(body):
    return ScriptVersion(version_number=1, body=body, source="UPLOAD", created_at="2026-11-12T10:00:00Z")


class CurrentScriptText(unittest.IsolatedAsyncioTestCase):
    async def text(self, file, versions=()):
        with patch.object(srs, "_script_file", new=AsyncMock(return_value=file)), \
             patch.object(srs, "_versions", new=AsyncMock(return_value=list(versions))):
            return await srs.current_script_text({"id": uuid4()})

    async def test_the_uploaded_script_is_compared_once_it_has_been_read(self):
        file = {"doc_text_status": "COMPLETED", "doc_text": "Open on the hangar.\n\n\nWe are live."}
        self.assertIn("We are live.", await self.text(file))

    async def test_a_script_not_read_yet_falls_back_to_the_last_version_sent(self):
        file = {"doc_text_status": "PROCESSING", "doc_text": None}
        self.assertEqual(await self.text(file, [version("The version that went out")]), "The version that went out")

    async def test_no_script_at_all_has_nothing_to_compare(self):
        self.assertEqual(await self.text(None), "")


class DuplicatesEndpoint(unittest.IsolatedAsyncioTestCase):
    async def look_up(self, row, script_text="", stage=True):
        found = DuplicateReport()
        with patch.object(router.svc, "_get_owned_row", new=AsyncMock(return_value=row)), \
             patch.object(router.svc, "script_stage_on", return_value=stage), \
             patch.object(srs, "current_script_text", new=AsyncMock(return_value=script_text)), \
             patch.object(router.duplicate_service, "find_script", new=AsyncMock(return_value=found)) as by_script, \
             patch.object(router.duplicate_service, "find", new=AsyncMock(return_value=found)) as by_post:
            await router.find_duplicates(uuid4(), USER)
        return by_script, by_post

    async def test_a_video_post_at_its_script_stage_is_compared_by_its_script(self):
        by_script, by_post = await self.look_up({"script_status": None}, script_text="Hello there")
        by_script.assert_awaited_once()
        by_post.assert_not_awaited()

    async def test_without_a_script_yet_there_is_nothing_to_find(self):
        by_script, by_post = await self.look_up({"script_status": "DRAFT"}, script_text="")
        by_script.assert_not_awaited()
        by_post.assert_not_awaited()

    async def test_once_the_script_is_approved_the_post_is_compared_as_a_post(self):
        by_script, by_post = await self.look_up({"script_status": "APPROVED"}, script_text="Hello there")
        by_post.assert_awaited_once()
        by_script.assert_not_awaited()

    async def test_other_posts_are_compared_as_before(self):
        by_script, by_post = await self.look_up({"script_status": None}, stage=False)
        by_post.assert_awaited_once()
        by_script.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
