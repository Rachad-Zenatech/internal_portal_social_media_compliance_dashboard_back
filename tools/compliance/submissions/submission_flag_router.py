"""Creator dismissal requests and reviewers' calls on compliance flags."""
from typing import Annotated, List
from uuid import UUID
from fastapi import APIRouter, Depends, status
from models.compliance.flag_outcome_model import FlagOutcome
from services.compliance.submissions import flag_outcome_service
from models.compliance.submission_model import Submission
from models.compliance.flag_review_model import (
    CreatorFlagDismissalRequest, CreatorFlagDismissalRejection, FindingVerdict,
    FlagConfirmRequest, FlagDismissRequest, FlagReview,
)
from services.compliance.submissions import flag_review_service
from services.shared.auth.auth_service import get_current_user_id_dependency

router = APIRouter()
CurrentUser = Annotated[UUID, Depends(get_current_user_id_dependency)]


@router.get("/{submission_id}/flags", response_model=List[FlagReview])
async def list_flag_reviews(submission_id: UUID, user_id: CurrentUser):
    """Reviewers' dismissals of automated compliance flags, on every version."""
    return await flag_review_service.list_for(submission_id, user_id)


@router.get("/{submission_id}/creator-flag-dismissals", response_model=List[CreatorFlagDismissalRequest])
async def creator_flag_dismissals(submission_id: UUID, user_id: CurrentUser):
    return await flag_review_service.creator_requests_for(submission_id, user_id)


@router.put("/{submission_id}/creator-flag-dismissals/{rule_id}", response_model=CreatorFlagDismissalRequest)
async def request_creator_flag_dismissal(submission_id: UUID, rule_id: UUID, data: FlagDismissRequest, user_id: CurrentUser):
    return await flag_review_service.request_dismissal(submission_id, rule_id, user_id, data)


@router.put("/{submission_id}/creator-flag-dismissals/{rule_id}/approve", response_model=FlagReview)
async def approve_creator_flag_dismissal(submission_id: UUID, rule_id: UUID, user_id: CurrentUser):
    return await flag_review_service.approve_creator_request(submission_id, rule_id, user_id)


@router.put("/{submission_id}/creator-flag-dismissals/{rule_id}/reject", response_model=Submission)
async def reject_creator_flag_dismissal(
    submission_id: UUID, rule_id: UUID, data: CreatorFlagDismissalRejection, user_id: CurrentUser,
):
    """Decline a creator's dismissal request and send the post back through the standard changes flow."""
    return await flag_review_service.reject_creator_request(submission_id, rule_id, user_id, data)


@router.put("/{submission_id}/flags/{rule_id}", response_model=FlagReview)
async def dismiss_flag(submission_id: UUID, rule_id: UUID, data: FlagDismissRequest, user_id: CurrentUser):
    """Dismiss a flag on the version in review as a false alarm, with a reason. The score itself isn't changed."""
    return await flag_review_service.dismiss(submission_id, rule_id, user_id, data)


@router.delete("/{submission_id}/flags/{rule_id}", status_code=status.HTTP_204_NO_CONTENT)
async def restore_flag(submission_id: UUID, rule_id: UUID, user_id: CurrentUser):
    """Undo a dismissal on the version in review."""
    await flag_review_service.undo(submission_id, rule_id, user_id)


@router.get("/{submission_id}/flag-verdicts", response_model=List[FindingVerdict])
async def list_flag_verdicts(submission_id: UUID, user_id: CurrentUser):
    """Reviewers' calls on the automated flags (right, or a false alarm), on every version."""
    return await flag_review_service.verdicts_for(submission_id, user_id)


@router.put("/{submission_id}/flags/{rule_id}/confirm", response_model=FindingVerdict)
async def confirm_flag(submission_id: UUID, rule_id: UUID, data: FlagConfirmRequest, user_id: CurrentUser):
    """Say a flag on the version in review is right. It's kept as an example for the contextual check."""
    return await flag_review_service.confirm(submission_id, rule_id, user_id, data)


@router.delete("/{submission_id}/flags/{rule_id}/confirm", status_code=status.HTTP_204_NO_CONTENT)
async def unconfirm_flag(submission_id: UUID, rule_id: UUID, user_id: CurrentUser):
    """Take back a confirmation."""
    await flag_review_service.unconfirm(submission_id, rule_id, user_id)


@router.get("/{submission_id}/flag-outcomes", response_model=List[FlagOutcome])
async def flag_outcomes(submission_id: UUID, user_id: CurrentUser):
    return await flag_outcome_service.list_for(submission_id, user_id)
