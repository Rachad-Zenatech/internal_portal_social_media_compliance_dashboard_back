"""Router for quick AI checking of submission source links."""

from uuid import UUID
from fastapi import APIRouter

from models.compliance.compliance_model import SourceCheck
from models.compliance.source_link_model import SourceLinkCheckRequest
from services.compliance.submissions import source_link_service
from tools.shared.core.auth_router import CurrentUser

router = APIRouter()


@router.post("/{submission_id}/check-source-link", response_model=SourceCheck)
async def check_source_link(submission_id: UUID, data: SourceLinkCheckRequest, user_id: CurrentUser):
    """Quickly fetch one source URL and check whether it supports the post's current material."""
    return await source_link_service.check_submission_source_link(submission_id, data.url, user_id)

