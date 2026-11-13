"""Router for people's calls on a post's sources (does a claim match the page? is the page a good source?) and the
standing each source's site has with reviewers."""

from typing import Annotated, List
from uuid import UUID

from fastapi import APIRouter, Depends, Query

from models.compliance.source_trust_model import SourceConfirmationRequest, SourceConfirmationsResponse
from services.compliance.submissions import source_trust_service
from services.shared.auth.auth_service import get_current_user_id_dependency

CurrentUser = Annotated[UUID, Depends(get_current_user_id_dependency)]
router = APIRouter()


@router.get("/{submission_id}/source-confirmations", response_model=SourceConfirmationsResponse)
async def get_source_confirmations(submission_id: UUID, user_id: CurrentUser, url: Annotated[List[str], Query()] = []):
    """Everyone's calls on this post's sources, and what reviewers have said about each source's site."""
    return await source_trust_service.for_submission(submission_id, user_id, url[:20])


@router.post("/{submission_id}/source-confirmations", response_model=SourceConfirmationsResponse)
async def confirm_source(submission_id: UUID, data: SourceConfirmationRequest, user_id: CurrentUser):
    """Confirm (or take back) whether a claim matches its source, or rate the source itself."""
    return await source_trust_service.record(submission_id, user_id, data)
