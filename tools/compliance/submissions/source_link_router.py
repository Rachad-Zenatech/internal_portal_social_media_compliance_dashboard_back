"""Router for quick AI checking of submission source links."""

from typing import Annotated, Optional
from uuid import UUID
from fastapi import APIRouter, Depends, File, HTTPException, UploadFile

from models.compliance.compliance_model import SourceCheck
from models.compliance.source_link_model import (
    ImageSourceScanJobResponse, PastedSourceCheckRequest, SourceLinkCheckRequest,
)
from services.compliance.submissions import image_source_service, source_link_service
from services.shared.auth.auth_service import get_current_user_id_dependency

CurrentUser = Annotated[UUID, Depends(get_current_user_id_dependency)]
router = APIRouter()


@router.post("/{submission_id}/check-source-link", response_model=SourceCheck)
async def check_source_link(submission_id: UUID, data: SourceLinkCheckRequest, user_id: CurrentUser):
    """Quickly fetch one source URL and check whether it supports the post's current material."""
    return await source_link_service.check_submission_source_link(submission_id, data.url, user_id)


@router.post("/{submission_id}/check-pasted-source", response_model=SourceCheck)
async def check_pasted_source(submission_id: UUID, data: PastedSourceCheckRequest, user_id: CurrentUser):
    """Check the post's claims against a whole article pasted in, and where in it each claim is found."""
    return await source_link_service.check_submission_pasted_source(
        submission_id, user_id, data.text, title=data.title, url=data.url,
    )


@router.post("/{submission_id}/scan-image-sources", response_model=ImageSourceScanJobResponse)
async def scan_image_sources(
    submission_id: UUID,
    user_id: CurrentUser,
    file: Optional[UploadFile] = File(None),
    file_id: Optional[UUID] = None,
):
    """Queue image source scanning, web lookup, and scraping as a background job."""
    image_bytes: Optional[bytes] = None
    mime_type: Optional[str] = None
    if file is not None:
        if not (file.content_type or "").startswith("image/"):
            raise HTTPException(status_code=422, detail="Upload an image file (PNG, JPG, WebP).")
        image_bytes = await file.read()
        if not image_bytes:
            raise HTTPException(status_code=422, detail="Uploaded image is empty.")
        mime_type = file.content_type
    return await image_source_service.start_scan_job(
        submission_id=submission_id,
        user_id=user_id,
        image_bytes=image_bytes,
        mime_type=mime_type,
        file_id=file_id,
    )


@router.get("/{submission_id}/scan-image-sources/{job_id}", response_model=ImageSourceScanJobResponse)
async def get_image_source_scan_job(submission_id: UUID, job_id: str, user_id: CurrentUser):
    """Poll the status and result of a background image source scan job."""
    return await image_source_service.get_scan_job(submission_id=submission_id, job_id=job_id, user_id=user_id)

