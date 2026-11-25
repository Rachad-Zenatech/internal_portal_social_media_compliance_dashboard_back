"""Attributed reviewer edits to text-only posts."""
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends

from models.compliance.post_text_review_model import PostTextReview, PostTextSave
from services.compliance.submissions import post_text_review
from services.shared.auth.auth_service import get_current_user_id_dependency

router = APIRouter()
CurrentUser = Annotated[UUID, Depends(get_current_user_id_dependency)]


@router.get("/{submission_id}/text-review", response_model=PostTextReview)
async def get_text_review(submission_id: UUID, user_id: CurrentUser):
    return await post_text_review.get_review(submission_id, user_id)


@router.post("/{submission_id}/text-review", response_model=PostTextReview)
async def save_text_review(submission_id: UUID, data: PostTextSave, user_id: CurrentUser):
    return await post_text_review.save_review(submission_id, user_id, data)
