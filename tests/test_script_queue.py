import asyncio
import unittest
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

from services.compliance import script_review_service as srs

USER = uuid4()


def script_row(**over):
    row = {
        "id": uuid4(), "reference_code": "SUB-1", "title": "Test", "content_type": "VIDEO", "platforms": ["INSTAGRAM"],
        "brand_code": "ZENADRONE", "audience": "GENERAL", "region": "GLOBAL", "rule_sets": [], "created_by": uuid4(),
        "approval_needed_by": None, "script_status": "IN_REVIEW", "updated_at": datetime.now(timezone.utc),
        "creator_name": "Creator", "version_number": 1, "sent_at": datetime.now(timezone.utc), "script_body": "Hello.",
        "check_result": {"rulebook_version": "old", "score": 90, "traffic_light": "GREEN"}, "reviewed_at": None,
    }
    row.update(over)
    return row


class QueueTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        srs._refreshing.clear()

    async def run_queue(self, rows, refresh):
        check = AsyncMock(return_value={"score": 50})
        with patch.object(srs, "fetch_all", new=AsyncMock(return_value=rows)), \
             patch.object(srs.subs, "_can_see_team_content", new=AsyncMock(return_value=True)), \
             patch.object(srs.subs, "_is_super_admin", new=AsyncMock(return_value=False)), \
             patch.object(srs.subs, "_can_review", new=AsyncMock(return_value=True)), \
             patch.object(srs.subs, "_brand_scope", new=AsyncMock(return_value=None)), \
             patch.object(srs, "rulebook_version", new=AsyncMock(return_value="new")), \
             patch.object(srs, "check_text", new=check), \
             patch.object(srs, "_refresh_in_background", new=refresh):
            items = await srs.queue(USER, "review")
        return items, check

    async def test_the_list_answers_from_what_is_saved_without_checking_inline(self):
        refresh = MagicMock()
        items, check = await self.run_queue([script_row()], refresh)
        check.assert_not_awaited()
        self.assertEqual(items[0].score, 90)  # the saved score, not a new one
        self.assertEqual(len(refresh.call_args.args[0]), 1)  # ...and the outdated one is refreshed afterwards

    async def test_an_up_to_date_script_is_not_refreshed(self):
        refresh = MagicMock()
        await self.run_queue([script_row(check_result={"rulebook_version": "new", "score": 80})], refresh)
        self.assertEqual(refresh.call_args.args[0], [])


class BackgroundRefreshTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        srs._refreshing.clear()

    async def test_the_same_script_is_only_refreshed_once_at_a_time(self):
        row = script_row()
        refresh = AsyncMock()
        with patch.object(srs, "_refresh_stale_scripts", new=refresh):
            srs._refresh_in_background([row])
            srs._refresh_in_background([row])
            await asyncio.sleep(0)
        self.assertEqual(refresh.await_count, 1)

    async def test_a_refresh_saves_the_new_check(self):
        row = script_row()
        saved = AsyncMock()
        with patch.object(srs, "check_text", new=AsyncMock(return_value={"score": 70})), patch.object(srs, "execute", new=saved):
            srs._refreshing.add((row["id"], row["version_number"]))
            await srs._refresh_stale_scripts([row])
        self.assertIn("UPDATE content_script_versions", saved.await_args.args[0])
        self.assertNotIn((row["id"], row["version_number"]), srs._refreshing)

    async def test_a_failed_refresh_does_not_stop_the_rest(self):
        a, b = script_row(), script_row()
        check = AsyncMock(side_effect=[RuntimeError("boom"), {"score": 70}])
        saved = AsyncMock()
        with patch.object(srs, "check_text", new=check), patch.object(srs, "execute", new=saved):
            await srs._refresh_stale_scripts([a, b])
        self.assertEqual(saved.await_count, 1)


class CheckTextFlagTests(unittest.IsolatedAsyncioTestCase):
    async def check(self, source):
        evaluation = SimpleNamespace(source=source) if source else None
        result = SimpleNamespace(
            violations=[SimpleNamespace(evaluation=evaluation)], ai_unavailable=source == "UNAVAILABLE",
            model_dump=lambda mode="json": {"score": 100},
        )
        with patch.object(srs, "check_compliance", new=AsyncMock(return_value=result)), \
             patch.object(srs.subs, "check_request", return_value=None):
            return await srs.check_text({"title": "T"}, "Hello.")

    async def test_a_script_the_ai_could_not_read_is_flagged(self):
        self.assertTrue((await self.check("UNAVAILABLE"))["ai_unavailable"])

    async def test_a_script_the_ai_read_is_not(self):
        self.assertFalse((await self.check("AI"))["ai_unavailable"])
        self.assertFalse((await self.check(None))["ai_unavailable"])


if __name__ == "__main__":
    unittest.main()
