"""Review decisions, hand-offs and comments for content submissions."""
from typing import Annotated, List
from uuid import UUID
from fastapi import APIRouter, Depends, status
from models.compliance.submission_model import (
    DelegateRequest, Delegation, ReopenRequest, RescheduleRequest, ReviewComment,
    ReviewCommentCreate, ReviewCommentUpdate, ReviewDecisionRequest, Submission,
)
from models.compliance.caption_review_model import CaptionEditRequest, CaptionReview
from models.compliance.post_approval_model import PublishingAccess
from services.compliance.submissions import caption_review_service, delegation_service
from services.compliance.submissions import post_approval_suggestions
from services.compliance.submissions import submission_service as svc
from services.shared.auth.auth_service import get_current_user_id_dependency

from tools.compliance.submissions.post_text_review_router import router as text_review_router

router = APIRouter()
router.include_router(text_review_router)
CurrentUser = Annotated[UUID, Depends(get_current_user_id_dependency)]


@router.get("/{submission_id}/publishing-access", response_model=PublishingAccess)
async def publishing_access(submission_id: UUID, user_id: CurrentUser):
    return await post_approval_suggestions.publishing_for_viewer(submission_id, user_id)


@router.post("/{submission_id}/review", response_model=Submission)
async def review_submission(submission_id: UUID, data: ReviewDecisionRequest, user_id: CurrentUser):
    """Reviewer decision on the version in review: approve (schedule it, or publish now) or request changes.
    Reviewers can't decide on their own uploads; the creator is notified either way."""
    return await svc.decide_review(submission_id, user_id, data)


@router.post("/{submission_id}/delegate", response_model=Submission)
async def delegate_review(submission_id: UUID, data: DelegateRequest, user_id: CurrentUser):
    """Reviewer hands the post in review to another reviewer; the delegator keeps seeing it and can still decide."""
    return await delegation_service.delegate(submission_id, user_id, data)


@router.get("/{submission_id}/delegations", response_model=List[Delegation])
async def submission_delegations(submission_id: UUID, user_id: CurrentUser):
    """Every review hand-off on this post."""
    return await delegation_service.submission_delegations(submission_id, user_id)


@router.post("/{submission_id}/reopen", response_model=Submission)
async def reopen_approval(submission_id: UUID, data: ReopenRequest, user_id: CurrentUser):
    """Reviewer takes back an approval and sends the content to its creator as changes requested."""
    return await svc.reopen_approved(submission_id, user_id, data)


@router.post("/{submission_id}/reschedule", response_model=Submission)
async def reschedule(submission_id: UUID, data: RescheduleRequest, user_id: CurrentUser):
    """Reviewer moves approved content to a new publish time; the approval stands."""
    return await svc.reschedule_approved(submission_id, user_id, data)


@router.get("/{submission_id}/comments", response_model=List[ReviewComment])
async def list_comments(submission_id: UUID, user_id: CurrentUser):
    """Review comments on every version, oldest first."""
    return await svc.list_comments(submission_id, user_id)


@router.post("/{submission_id}/comments", response_model=ReviewComment, status_code=status.HTTP_201_CREATED)
async def add_comment(submission_id: UUID, data: ReviewCommentCreate, user_id: CurrentUser):
    """Add a comment on the current version (reviewers and the creator), optionally pinned to a video time range."""
    return await svc.add_comment(submission_id, user_id, data)


@router.patch("/{submission_id}/comments/{comment_id}", response_model=ReviewComment)
async def update_comment(submission_id: UUID, comment_id: UUID, data: ReviewCommentUpdate, user_id: CurrentUser):
    """Mark a comment resolved (or reopen it)."""
    return await svc.set_comment_resolved(submission_id, comment_id, user_id, data.resolved)


@router.delete("/{submission_id}/comments/{comment_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_comment(submission_id: UUID, comment_id: UUID, user_id: CurrentUser):
    """Delete your own comment."""
    await svc.delete_comment(submission_id, comment_id, user_id)


@router.get("/{submission_id}/caption-review", response_model=CaptionReview)
async def get_caption_review(submission_id: UUID, user_id: CurrentUser):
    """Current captions and the attributed before/after history, visible to post participants."""
    return await caption_review_service.get_caption_review(submission_id, user_id)


@router.patch("/{submission_id}/caption-review", response_model=CaptionReview)
async def save_review_caption(submission_id: UUID, data: CaptionEditRequest, user_id: CurrentUser):
    """Reviewers and suggesting reviewers save caption wording directly, with conflict detection."""
    return await caption_review_service.save_caption(submission_id, user_id, data)