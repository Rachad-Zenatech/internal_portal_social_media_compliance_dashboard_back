"""Router for quick AI checking of submission source links."""

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends

from models.compliance.compliance_model import SourceCheck
from models.compliance.source_link_model import SourceLinkCheckRequest
from services.compliance.submissions import source_link_service
from services.shared.auth.auth_service import get_current_user_id_dependency

router = APIRouter()
CurrentUser = Annotated[UUID, Depends(get_current_user_id_dependency)]


@router.post("/{submission_id}/check-source-link", response_model=SourceCheck)
async def check_source_link(submission_id: UUID, data: SourceLinkCheckRequest, user_id: CurrentUser):
    """Quickly fetch one source URL and check whether it supports the post's current material."""
    return await source_link_service.check_submission_source_link(submission_id, data.url, user_id)

