"""Script Review Router
The script stage of video posts: send the script, review and edit it, accept the reviewer's edits.
"""

from typing import Annotated, Any, Dict, List, Literal, Optional
from uuid import UUID

from fastapi import APIRouter, Depends, Query

from models.compliance.script_review_model import (
    FlagFixRequest,
    FlagFixSuggestion,
    ScriptSubmitRequest,
    ScriptHighlight,
    ScriptHighlightsRequest,
    ScriptResolutionsRequest,
    ScriptCheckRequest,
    ScriptDecisionRequest,
    ScriptQueueItem,
    ScriptReviseRequest,
    ScriptStage,
    ScriptSuggestionRequest,
    SuggesterDashboard,
)
from services.compliance.evaluation import script_review_service as svc
from services.shared.auth.auth_service import get_current_user_id_dependency

router = APIRouter(prefix="/submissions", tags=["Script Review"])

CurrentUser = Annotated[UUID, Depends(get_current_user_id_dependency)]


@router.post("/scripts/suggest-fix", response_model=FlagFixSuggestion)
async def suggest_fix(req: FlagFixRequest, user_id: CurrentUser):
    """A compliant rewrite of one flagged passage of a script, for the creator to use or not."""
    return await svc.suggest_fix(req)


@router.get("/scripts/suggester-dashboard", response_model=SuggesterDashboard)
async def suggester_dashboard(user_id: CurrentUser):
    """A suggesting reviewer's dashboard: what is sent to them, and what they have done on it."""
    return await svc.suggester_dashboard(user_id)


@router.get("/scripts/queue", response_model=List[ScriptQueueItem])
async def script_queue(user_id: CurrentUser, tab: Literal["review", "sent_back", "done"] = Query("review")):
    """Scripts for a Content Screener tab: waiting for a reviewer, sent back to the creator, or approved."""
    return await svc.queue(user_id, tab)


@router.get("/{submission_id}/script", response_model=ScriptStage)
async def get_script(submission_id: UUID, user_id: CurrentUser):
    """The post's script stage: every version, the reviewer's decisions and what the viewer can do."""
    return await svc.get_stage(submission_id, user_id)


@router.post("/{submission_id}/script/submit", response_model=ScriptStage)
async def submit_script(submission_id: UUID, user_id: CurrentUser, body: Optional[ScriptSubmitRequest] = None):
    """Send the uploaded script for review. The same script already sent for the same place and publish date is
    refused (409 DUPLICATE_SCRIPT); for another date it needs confirm_repost (409 REPOST_CONFIRM)."""
    return await svc.submit(submission_id, user_id, confirm_repost=bool(body and body.confirm_repost))


@router.post("/{submission_id}/script/decision", response_model=ScriptStage)
async def decide_script(submission_id: UUID, req: ScriptDecisionRequest, user_id: CurrentUser):
    """Approve the script, or send it back with a note and/or the reviewer's edited version."""
    return await svc.decide(submission_id, user_id, req)


@router.put("/{submission_id}/script/suggestion", response_model=ScriptStage)
async def save_script_suggestion(submission_id: UUID, req: ScriptSuggestionRequest, user_id: CurrentUser):
    """A suggesting reviewer saves their edited wording and notes for the script in review (theirs alone for now)."""
    return await svc.save_suggestion(submission_id, user_id, req)


@router.delete("/{submission_id}/script/suggestion", response_model=ScriptStage)
async def discard_script_suggestion(submission_id: UUID, user_id: CurrentUser):
    """Take back a suggesting reviewer's own suggestion on the script."""
    return await svc.discard_suggestion(submission_id, user_id)


@router.post("/{submission_id}/script/accept", response_model=ScriptStage)
async def accept_script_edits(submission_id: UUID, user_id: CurrentUser):
    """Accept the reviewer's edits; the script is approved."""
    return await svc.accept(submission_id, user_id)


@router.post("/{submission_id}/script/revise", response_model=ScriptStage)
async def revise_script(submission_id: UUID, req: ScriptReviseRequest, user_id: CurrentUser):
    """Send an edited script back to the reviewer."""
    return await svc.revise(submission_id, user_id, req)


@router.post("/{submission_id}/script/precheck", response_model=ScriptStage)
async def precheck_script(submission_id: UUID, user_id: CurrentUser):
    """Creator: check the uploaded script against the rulebook before sending it (the result is kept)."""
    return await svc.precheck(submission_id, user_id)


@router.post("/{submission_id}/script/check", response_model=Dict[str, Any])
async def check_script(submission_id: UUID, req: ScriptCheckRequest, user_id: CurrentUser):
    """Check wording being edited, without saving it."""
    return await svc.preview_check(submission_id, user_id, req.text)


@router.get("/{submission_id}/script/highlights", response_model=List[ScriptHighlight])
async def get_script_highlights(submission_id: UUID, user_id: CurrentUser, version: int = Query(..., ge=1)):
    """The reviewer's highlights and notes on a script version (the creator sees them too)."""
    return await svc.get_highlights(submission_id, user_id, version)


@router.put("/{submission_id}/script/highlights", response_model=List[ScriptHighlight])
async def save_script_highlights(submission_id: UUID, req: ScriptHighlightsRequest, user_id: CurrentUser, version: int = Query(..., ge=1)):
    """Save the reviewer's highlights and notes on a script version."""
    return await svc.save_highlights(submission_id, user_id, version, req.highlights)


@router.post("/{submission_id}/script/highlights/resolve", response_model=List[ScriptHighlight])
async def resolve_script_highlights(
    submission_id: UUID, req: ScriptResolutionsRequest, user_id: CurrentUser, version: int = Query(..., ge=1)
):
    """The creator marks the reviewer's highlights they fixed, with the wording as it reads now."""
    return await svc.resolve_highlights(submission_id, user_id, version, req.resolutions)
