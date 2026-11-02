"""Script Review Router
The script stage of video posts: send the script, review and edit it, accept the reviewer's edits.
"""

from typing import Annotated, Any, Dict, List, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, Query

from models.compliance.script_review_model import (
    ScriptCheckRequest,
    ScriptDecisionRequest,
    ScriptQueueItem,
    ScriptReviseRequest,
    ScriptStage,
)
from services.compliance import script_review_service as svc
from services.shared.auth_service import get_current_user_id_dependency

router = APIRouter(prefix="/submissions", tags=["Script Review"])

CurrentUser = Annotated[UUID, Depends(get_current_user_id_dependency)]


@router.get("/scripts/queue", response_model=List[ScriptQueueItem])
async def script_queue(user_id: CurrentUser, tab: Literal["review", "sent_back", "done"] = Query("review")):
    """Scripts for a Content Screener tab: waiting for a reviewer, sent back to the creator, or approved."""
    return await svc.queue(user_id, tab)


@router.get("/{submission_id}/script", response_model=ScriptStage)
async def get_script(submission_id: UUID, user_id: CurrentUser):
    """The post's script stage: every version, the reviewer's decisions and what the viewer can do."""
    return await svc.get_stage(submission_id, user_id)


@router.post("/{submission_id}/script/submit", response_model=ScriptStage)
async def submit_script(submission_id: UUID, user_id: CurrentUser):
    """Send the uploaded script for review."""
    return await svc.submit(submission_id, user_id)


@router.post("/{submission_id}/script/decision", response_model=ScriptStage)
async def decide_script(submission_id: UUID, req: ScriptDecisionRequest, user_id: CurrentUser):
    """Approve the script, or send it back with a note and/or the reviewer's edited version."""
    return await svc.decide(submission_id, user_id, req)


@router.post("/{submission_id}/script/accept", response_model=ScriptStage)
async def accept_script_edits(submission_id: UUID, user_id: CurrentUser):
    """Accept the reviewer's edits; the script is approved."""
    return await svc.accept(submission_id, user_id)


@router.post("/{submission_id}/script/revise", response_model=ScriptStage)
async def revise_script(submission_id: UUID, req: ScriptReviseRequest, user_id: CurrentUser):
    """Send an edited script back to the reviewer."""
    return await svc.revise(submission_id, user_id, req)


@router.post("/{submission_id}/script/check", response_model=Dict[str, Any])
async def check_script(submission_id: UUID, req: ScriptCheckRequest, user_id: CurrentUser):
    """Check wording being edited, without saving it."""
    return await svc.preview_check(submission_id, user_id, req.text)
