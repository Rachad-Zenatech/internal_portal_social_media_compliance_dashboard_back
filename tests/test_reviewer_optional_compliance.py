import asyncio
from unittest.mock import AsyncMock, patch
import pytest
from fastapi import HTTPException
from services.compliance.submissions import review_approval_service as approval, submission_service as subs


@pytest.mark.parametrize("check", [None, {"review_caption_edited": True}, {"analysis_pending": True}, {"ai_unavailable": True}])
def test_approval_for_creator_updates_does_not_require_or_run_ai(check):
    async def scenario():
        row = {"id": "post", "latest_check": check}
        with patch.object(approval, "require_final_approver", AsyncMock()) as permission, \
             patch.object(subs, "_recheck_one", AsyncMock()) as recheck, \
             patch.object(subs, "compliance_rulebook_version", AsyncMock()) as rulebook, \
             patch.object(subs.manual_check_service, "require_current_check") as required:
            result, score = await approval.require_approval(row, "reviewer", False, for_creator_updates=True)
            assert result is row and score is None
            permission.assert_awaited_once_with(row, "reviewer", False)
            recheck.assert_not_awaited()
            rulebook.assert_not_awaited()
            required.assert_not_called()
    asyncio.run(scenario())


def test_creator_handoff_cannot_bypass_final_approver_permissions():
    async def scenario():
        with patch.object(subs.approval_path_service, "load_rules", AsyncMock(return_value=[])), \
             patch.object(subs, "_accounts", return_value=[]), \
             patch.object(subs, "_approvers", return_value=(["assigned"], ["Assigned reviewer"])), \
             patch.object(subs.delegation_service, "with_delegates", AsyncMock(return_value=["assigned"])), \
             patch.object(subs, "_may_approve", return_value=False):
            with pytest.raises(HTTPException) as error:
                await approval.require_approval({"id": "post"}, "other", False, for_creator_updates=True)
            assert error.value.status_code == 403
    asyncio.run(scenario())


def test_legacy_direct_approval_retains_current_check_validation():
    async def scenario():
        with patch.object(subs, "compliance_rulebook_version", AsyncMock(return_value="current")), \
             patch.object(subs.manual_check_service, "require_current_check", side_effect=HTTPException(422, "Required")) as required:
            with pytest.raises(HTTPException):
                await approval.require_approval({"id": "post", "latest_check": None}, "reviewer", False)
            required.assert_called_once()
    asyncio.run(scenario())
