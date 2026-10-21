"""Content Submission Router
Create, save and resume drafts, attach files, submit for compliance review and view history.
"""

from typing import Annotated, List, Literal, Optional
from uuid import UUID

from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, UploadFile, status
from fastapi.responses import FileResponse

from models.sentiment_model import SentimentAnalysis
from models.submission_model import (
    ReviewQueueItem,
    Submission,
    SubmissionCreate,
    SubmissionFile,
    SubmissionHistory,
    SubmissionOptions,
    SubmissionSummary,
    SubmissionUpdate,
    SubmitResult,
)
from services import sentiment_service
from services import submission_service as svc
from services.auth_service import get_current_user_id_dependency

router = APIRouter(prefix="/submissions", tags=["Content Submissions"])

CurrentUser = Annotated[UUID, Depends(get_current_user_id_dependency)]


@router.get("/options", response_model=SubmissionOptions)
async def get_options(_user_id: CurrentUser):
    """Brands, connected publishing accounts and the allowed values for each field."""
    return await svc.get_options()


@router.get("/review-queue", response_model=List[ReviewQueueItem])
async def review_queue(user_id: CurrentUser):
    """Submitted videos for the Content Screener queue (declared before /{submission_id} so it isn't parsed as an id)."""
    return await svc.review_queue(user_id)


@router.get("", response_model=List[SubmissionSummary])
async def list_my_submissions(user_id: CurrentUser, status_filter: Optional[str] = Query(None, alias="status")):
    """The current user's submissions, newest activity first."""
    return await svc.list_submissions(user_id, status_filter)


@router.post("", response_model=Submission, status_code=status.HTTP_201_CREATED)
async def create_submission(data: SubmissionCreate, user_id: CurrentUser):
    """Start a draft. It gets a unique reference code (e.g. SUB-1001)."""
    return await svc.create_submission(data, user_id)


@router.get("/{submission_id}", response_model=Submission)
async def get_submission(submission_id: UUID, user_id: CurrentUser):
    return await svc.get_submission(submission_id, user_id)


@router.patch("/{submission_id}", response_model=Submission)
async def update_submission(submission_id: UUID, data: SubmissionUpdate, user_id: CurrentUser):
    """Save draft fields. Only drafts and submissions with changes requested can be edited."""
    return await svc.update_submission(submission_id, data, user_id)


@router.delete("/{submission_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_draft(submission_id: UUID, user_id: CurrentUser):
    await svc.delete_draft(submission_id, user_id)


@router.post("/{submission_id}/files", response_model=SubmissionFile, status_code=status.HTTP_201_CREATED)
async def upload_file(
    submission_id: UUID,
    user_id: CurrentUser,
    file: UploadFile = File(...),
    kind: Literal["MAIN", "SCRIPT", "IMAGE", "SUPPORTING"] = Form(...),
):
    """Attach a content asset, script, image or supporting document (multipart/form-data)."""
    return await svc.add_file(submission_id, kind, file, user_id)


@router.get("/{submission_id}/files/{file_id}")
async def download_file(submission_id: UUID, file_id: UUID, user_id: CurrentUser):
    """Stream a file for preview. Supports range requests, so videos can seek."""
    path, mime, name = await svc.get_file_for_download(submission_id, file_id, user_id)
    return FileResponse(path, media_type=mime, filename=name, content_disposition_type="inline")


@router.post("/{submission_id}/files/{file_id}/transcribe", response_model=SubmissionFile, status_code=status.HTTP_202_ACCEPTED)
async def transcribe_file(submission_id: UUID, file_id: UUID, user_id: CurrentUser):
    """Regenerate a video's speech transcript in the background (it is generated automatically on upload)."""
    return await svc.retranscribe(submission_id, file_id, user_id)


@router.delete("/{submission_id}/files/{file_id}", status_code=status.HTTP_204_NO_CONTENT)
async def remove_file(submission_id: UUID, file_id: UUID, user_id: CurrentUser):
    await svc.remove_file(submission_id, file_id, user_id)


@router.post("/{submission_id}/check", response_model=Submission)
async def run_initial_check(submission_id: UUID, user_id: CurrentUser):
    """Screen the title and caption against the rulebook without submitting."""
    return await svc.run_initial_check(submission_id, user_id)


@router.post("/{submission_id}/submit", response_model=SubmitResult)
async def submit_for_review(submission_id: UUID, user_id: CurrentUser):
    """Validate required information, snapshot a new version and create a review job."""
    submission, job = await svc.submit_for_review(submission_id, user_id)
    return SubmitResult(submission=submission, review_job=job)


@router.post("/{submission_id}/withdraw", response_model=Submission)
async def withdraw(submission_id: UUID, user_id: CurrentUser):
    """Pull a submission back from review to edit it."""
    return await svc.withdraw(submission_id, user_id)


@router.get("/{submission_id}/history", response_model=SubmissionHistory)
async def get_history(submission_id: UUID, user_id: CurrentUser):
    """Versions, review jobs and the activity log."""
    return await svc.get_history(submission_id, user_id)


@router.get("/{submission_id}/sentiment", response_model=Optional[SentimentAnalysis])
async def get_sentiment(submission_id: UUID, user_id: CurrentUser):
    """Latest sentiment analysis (or null). While it runs, poll this for the status."""
    await svc.get_submission(submission_id, user_id)  # access check
    return await sentiment_service.get_latest(submission_id)


@router.post("/{submission_id}/sentiment", response_model=SentimentAnalysis, status_code=status.HTTP_202_ACCEPTED)
async def request_sentiment(submission_id: UUID, user_id: CurrentUser):
    """Analyse the current content's tone and reputational risk. Reuses the last result if nothing changed;
    videos wait for their transcript. Advisory only."""
    sub = await svc.get_submission(submission_id, user_id)
    if not (sub.title or "").strip() and not (sub.caption_text or "").strip() and not sub.files:
        raise HTTPException(status_code=422, detail="Add a title, caption or video before running sentiment analysis")
    analysis_id = await sentiment_service.request_analysis(submission_id, user_id)
    return await sentiment_service.get_analysis(analysis_id)

