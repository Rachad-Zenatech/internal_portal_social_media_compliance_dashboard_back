"""Review tab membership includes approved scripts without rerunning their analysis."""
import unittest
from unittest.mock import AsyncMock, patch
from uuid import uuid4
from services.compliance.evaluation import script_queue_service as service, script_review_service as scripts
from tools.compliance.submissions import script_review_router as router


class ScriptQueueSummaryTests(unittest.IsolatedAsyncioTestCase):
    async def test_approved_standalone_and_batch_scripts_are_returned(self):
        user, standalone, batched, batch = uuid4(), uuid4(), uuid4(), uuid4()
        rows = [{"id": standalone, "script_status": "APPROVED", "batch_id": None},
                {"id": batched, "script_status": "APPROVED", "batch_id": batch}]
        with patch.object(service.subs, "_can_see_team_content", AsyncMock(return_value=True)), \
             patch.object(service.subs, "_brand_scope", AsyncMock(return_value=["ZENATECH"])), \
             patch.object(service, "fetch_all", AsyncMock(return_value=rows)) as fetch, \
             patch.object(scripts, "_refresh_in_background") as refresh:
            result = await service.summary(user)
        self.assertEqual([item.id for item in result], [standalone, batched])
        self.assertEqual(result[1].batch_id, batch)
        self.assertEqual(fetch.await_args.args[1:], (["ZENATECH"], user))
        self.assertIn("s.deleted_at IS NULL", fetch.await_args.args[0])
        self.assertIn("s.script_status = 'APPROVED'", fetch.await_args.args[0])
        refresh.assert_not_called()

    async def test_no_team_access_returns_no_queue_records(self):
        with patch.object(service.subs, "_can_see_team_content", AsyncMock(return_value=False)), \
             patch.object(service.subs, "_brand_scope", AsyncMock(return_value=[])), \
             patch.object(service, "fetch_all", AsyncMock()) as fetch:
            self.assertEqual(await service.summary(uuid4()), [])
        fetch.assert_not_awaited()

    async def test_route_uses_authenticated_user_for_membership(self):
        user = uuid4()
        with patch.object(service, "summary", AsyncMock(return_value=[])) as summary:
            self.assertEqual(await router.script_queue_summary(user), [])
        summary.assert_awaited_once_with(user)

    def test_counts_and_full_queue_use_identical_status_membership(self):
        self.assertIs(scripts._QUEUE_TABS, service.QUEUE_TABS)
