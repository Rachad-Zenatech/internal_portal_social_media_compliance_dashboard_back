"""Content Submission Router
Create, save and resume drafts, attach files, submit for compliance review and view history.
"""

from typing import Annotated, Dict, List, Literal, Optional
from uuid import UUID

from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, UploadFile, status
from fastapi.responses import FileResponse

from models.canva_model import CanvaImportRequest, CanvaReimportRequest
from models.caption_model import CaptionSuggestions, CaptionSuggestRequest
from models.public_mood_model import MoodFeedbackIn, PublicMood
from models.sentiment_model import FindingFeedback, FindingFeedbackIn, SentimentAnalysis
from models.submission_model import (
    DashboardSummary,
    ReviewComment,
    ReviewCommentCreate,
    ReviewCommentUpdate,
    ReviewDecisionRequest,
    ReopenRequest,
    RescheduleRequest,
    QueueTab,
    ReviewQueueCounts,
    ReviewQueueItem,
    VersionView,
    Submission,
    SubmissionCreate,
    SubmissionFile,
    SubmissionHistory,
    SubmissionOptions,
    SubmissionSummary,
    SubmissionUpdate,
    SubmitResult,
)
from services.ai import ai_usage_service
from services.compliance import caption_service, public_mood_service, sentiment_service
from services.compliance import flag_review_service
from services.compliance import submission_service as svc
from models.flag_review_model import FlagDismissRequest, FlagReview
from services.shared.auth_service import get_current_user_id_dependency
from services.compliance.hashtag_library import HashtagLibrary

router = APIRouter(prefix="/submissions", tags=["Content Submissions"])

CurrentUser = Annotated[UUID, Depends(get_current_user_id_dependency)]


@router.get("/options", response_model=SubmissionOptions)
async def get_options(user_id: CurrentUser):
    """Brands, connected publishing accounts and the allowed values for each field."""
    return await svc.get_options(user_id)


@router.get("/hashtags", response_model=HashtagLibrary)
async def get_hashtag_library(
    user_id: CurrentUser,
    brand_code: Optional[str] = Query(None, max_length=50),
    audience: Optional[str] = Query(None, max_length=50),
    region: Optional[str] = Query(None, max_length=50),
    platforms: Annotated[List[str], Query()] = [],
    rule_sets: Annotated[List[str], Query()] = [],
):
    """Hashtags (and the company cashtag) suited to this brand, audience and these platforms, minus any the
    rules that apply would flag, and how many the strictest platform suits. Drafts can call it before they're saved."""
    return await caption_service.get_hashtag_library(brand_code, audience, region, platforms[:20], rule_sets[:10])


# scope=mine: only the user's own content, seen as its creator (a super admin viewing the app as a creator). It only
# ever narrows what's returned; it never grants access.
ViewScope = Literal["all", "mine"]


@router.get("/review-queue", response_model=List[ReviewQueueItem])
async def review_queue(
    user_id: CurrentUser,
    tab: QueueTab = Query("review"),
    search: Optional[str] = Query(None, max_length=200),
    scope: ViewScope = Query("all"),
):
    """Submitted posts for one Content Screener tab (declared before /{submission_id} so it isn't parsed as an id)."""
    return await svc.review_queue(user_id, tab, search, mine=scope == "mine")


@router.get("/review-queue/counts", response_model=ReviewQueueCounts)
async def review_queue_counts(user_id: CurrentUser, scope: ViewScope = Query("all")):
    """How many posts are in each Content Screener tab."""
    return await svc.review_queue_counts(user_id, mine=scope == "mine")


@router.get("/dashboard", response_model=DashboardSummary)
async def dashboard(user_id: CurrentUser, scope: ViewScope = Query("all")):
    """Recent activity and last-30-days compliance insights for the dashboard, over the content the user can see
    (declared before /{submission_id} so it isn't parsed as an id)."""
    return await svc.dashboard_summary(user_id, mine=scope == "mine")


@router.get("/ai-usage")
async def ai_usage(user_id: CurrentUser, submission_id: Optional[UUID] = Query(None)):
    """How many AI runs (sentiment, transcription) the current user has left in the rolling 24-hour window,
    and for one submission when given."""
    return await ai_usage_service.usage_for(user_id, submission_id)


@router.get("", response_model=List[SubmissionSummary])
async def list_my_submissions(
    user_id: CurrentUser,
    status_filter: Optional[str] = Query(None, alias="status"),
    scope: Literal["mine", "all"] = Query("mine", description="all = everyone's (super admins, Content Managers and reviewers)"),
):
    """The current user's submissions (or everyone's), needs-action first."""
    return await svc.list_submissions(user_id, status_filter, scope)


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
    """Delete a draft: its creator, super admins and Content Managers can."""
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


@router.post("/{submission_id}/canva/import", response_model=Submission)
async def import_from_canva(submission_id: UUID, data: CanvaImportRequest, user_id: CurrentUser):
    """Export a Canva design and add it as the main content file, exactly like an upload."""
    return await svc.import_from_canva(submission_id, user_id, data.design_id)


@router.post("/{submission_id}/canva/reimport", response_model=Submission)
async def reimport_from_canva(submission_id: UUID, data: CanvaReimportRequest, user_id: CurrentUser):
    """Replace the Canva-imported main file with the design's latest revision; optionally resubmit for review."""
    return await svc.reimport_from_canva(submission_id, user_id, data.resubmit)


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
    """Pull a submission back to draft to edit it: from review, or after approval for a late fix (the approval
    no longer applies and it's reviewed again)."""
    return await svc.withdraw(submission_id, user_id)


@router.get("/{submission_id}/versions/{version}", response_model=VersionView)
async def get_version(submission_id: UUID, version: int, user_id: CurrentUser):
    """One submitted version as it was: snapshot, video, automated check and decision."""
    return await svc.get_version(submission_id, version, user_id)


@router.get("/{submission_id}/history", response_model=SubmissionHistory)
async def get_history(submission_id: UUID, user_id: CurrentUser):
    """Versions, review jobs and the activity log."""
    return await svc.get_history(submission_id, user_id)


@router.get("/{submission_id}/sentiment", response_model=Optional[SentimentAnalysis])
async def get_sentiment(submission_id: UUID, user_id: CurrentUser, version: Optional[int] = Query(None, ge=0)):
    """Latest sentiment analysis (or null). While it runs, poll this for the status. With a version, the latest
    analysis of that version, as it was."""
    await svc.get_submission(submission_id, user_id)  # access check
    if version is not None:
        return await sentiment_service.get_for_version(submission_id, version, viewer=user_id)
    return await sentiment_service.get_latest(submission_id, viewer=user_id)


@router.post("/{submission_id}/sentiment", response_model=SentimentAnalysis, status_code=status.HTTP_202_ACCEPTED)
async def request_sentiment(submission_id: UUID, user_id: CurrentUser):
    """Analyse the current content's tone and reputational risk. Reuses the last result if nothing changed;
    videos wait for their transcript. Advisory only."""
    sub = await svc.get_submission(submission_id, user_id)
    if not (sub.title or "").strip() and not (sub.caption_text or "").strip() and not sub.files:
        raise HTTPException(status_code=422, detail="Add a title, caption or video before running sentiment analysis")
    analysis_id = await sentiment_service.request_analysis(submission_id, user_id)
    return await sentiment_service.get_analysis(analysis_id, viewer=user_id)


@router.post("/{submission_id}/caption-suggestions", response_model=CaptionSuggestions)
async def suggest_captions(submission_id: UUID, data: CaptionSuggestRequest, user_id: CurrentUser):
    """Three captions drafted from the post's images, video transcript and title, in the brand's voice, each
    already run through the compliance check. Nothing is saved; counts toward the AI limits."""
    await svc.get_submission(submission_id, user_id)  # access check
    return await caption_service.suggest(submission_id, user_id, data)


@router.get("/{submission_id}/public-mood", response_model=PublicMood)
async def get_public_mood(submission_id: UUID, user_id: CurrentUser):
    """How people online currently feel about the topics this post touches (social listening), with the saved
    summary if there is one. Topic data is shared and refreshed only when it goes stale. Advisory only."""
    await svc.get_submission(submission_id, user_id)  # access check
    return await public_mood_service.get_public_mood(submission_id, user_id)


@router.post("/{submission_id}/public-mood/summary", response_model=PublicMood)
async def summarise_public_mood(submission_id: UUID, user_id: CurrentUser):
    """An AI reading of how this post is likely to land given the current mood. Reused while neither the post nor
    the topics' moods have changed; otherwise counts toward the AI limits."""
    await svc.get_submission(submission_id, user_id)  # access check
    return await public_mood_service.summarise(submission_id, user_id)


@router.put("/{submission_id}/public-mood/topics/{topic_id}/dismissal", response_model=PublicMood)
async def dismiss_public_mood_topic(submission_id: UUID, topic_id: UUID, user_id: CurrentUser):
    """"This post isn't about that topic": leave it out here, and count it against the topic's keywords."""
    await svc.get_submission(submission_id, user_id)  # access check
    return await public_mood_service.dismiss_topic(submission_id, topic_id, user_id)


@router.delete("/{submission_id}/public-mood/topics/{topic_id}/dismissal", response_model=PublicMood)
async def restore_public_mood_topic(submission_id: UUID, topic_id: UUID, user_id: CurrentUser):
    await svc.get_submission(submission_id, user_id)  # access check
    return await public_mood_service.restore_topic(submission_id, topic_id, user_id)


@router.put("/{submission_id}/public-mood/summary/{summary_id}/feedback", response_model=PublicMood)
async def rate_public_mood(submission_id: UUID, summary_id: UUID, data: MoodFeedbackIn, user_id: CurrentUser):
    """Whether the summary helped; one vote per person, changeable."""
    await svc.get_submission(submission_id, user_id)  # access check
    return await public_mood_service.rate_summary(submission_id, summary_id, user_id, data)


@router.delete("/{submission_id}/public-mood/summary/{summary_id}/feedback", response_model=PublicMood)
async def unrate_public_mood(submission_id: UUID, summary_id: UUID, user_id: CurrentUser):
    await svc.get_submission(submission_id, user_id)  # access check
    return await public_mood_service.rate_summary(submission_id, summary_id, user_id, None)


@router.post("/{submission_id}/sentiment/{analysis_id}/schedule", response_model=SentimentAnalysis)
async def schedule_sentiment(submission_id: UUID, analysis_id: UUID, user_id: CurrentUser):
    """Run an analysis stopped by Gemini's daily limit again automatically once the limit resets."""
    await svc.get_submission(submission_id, user_id)
    return await sentiment_service.schedule_after_reset(submission_id, analysis_id)


@router.get("/{submission_id}/sentiment/{analysis_id}/frame")
async def sentiment_frame(submission_id: UUID, analysis_id: UUID, user_id: CurrentUser, t: float = Query(..., ge=0, le=36000)):
    """The analysed video's frame at t seconds, shown beside findings tied to that moment."""
    await svc.get_submission(submission_id, user_id)
    path = await sentiment_service.video_frame(submission_id, analysis_id, t)
    return FileResponse(path, media_type="image/jpeg", headers={"Cache-Control": "private, max-age=86400"})


@router.get("/{submission_id}/sentiment/{analysis_id}/images/{index}")
async def sentiment_image(submission_id: UUID, analysis_id: UUID, index: int, user_id: CurrentUser):
    """One of the images the analysis looked at (1 = the first), shown beside findings about it."""
    await svc.get_submission(submission_id, user_id)
    path, mime = await sentiment_service.analysed_image(submission_id, analysis_id, index)
    return FileResponse(path, media_type=mime, headers={"Cache-Control": "private, max-age=86400"})


async def _require_reviewer(user_id: UUID) -> None:
    if not await svc._can_review(user_id):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Only reviewers can rate findings")


@router.put("/{submission_id}/sentiment/{analysis_id}/findings/{finding_id}/feedback", response_model=Dict[str, FindingFeedback])
async def rate_finding(submission_id: UUID, analysis_id: UUID, finding_id: str, data: FindingFeedbackIn, user_id: CurrentUser):
    """A reviewer marks a finding useful or not an issue (their own vote; changing it replaces it)."""
    await svc.get_submission(submission_id, user_id)
    await _require_reviewer(user_id)
    return await sentiment_service.set_feedback(submission_id, analysis_id, finding_id, user_id, data)


@router.delete("/{submission_id}/sentiment/{analysis_id}/findings/{finding_id}/feedback", response_model=Dict[str, FindingFeedback])
async def clear_finding_rating(submission_id: UUID, analysis_id: UUID, finding_id: str, user_id: CurrentUser):
    """Take back the reviewer's own vote on a finding."""
    await svc.get_submission(submission_id, user_id)
    await _require_reviewer(user_id)
    return await sentiment_service.clear_feedback(submission_id, analysis_id, finding_id, user_id)


@router.post("/{submission_id}/review", response_model=Submission)
async def review_submission(submission_id: UUID, data: ReviewDecisionRequest, user_id: CurrentUser):
    """Reviewer decision on the version in review: approve (schedule it, or publish now) or request changes.
    Reviewers can't decide on their own uploads; the creator is notified either way."""
    return await svc.decide_review(submission_id, user_id, data)


@router.post("/{submission_id}/reopen", response_model=Submission)
async def reopen_approval(submission_id: UUID, data: ReopenRequest, user_id: CurrentUser):
    """Reviewer takes back an approval and sends the content to its creator as changes requested."""
    return await svc.reopen_approved(submission_id, user_id, data)


@router.post("/{submission_id}/reschedule", response_model=Submission)
async def reschedule(submission_id: UUID, data: RescheduleRequest, user_id: CurrentUser):
    """Reviewer moves approved content to a new publish time; the approval stands."""
    return await svc.reschedule_approved(submission_id, user_id, data)


@router.get("/{submission_id}/flags", response_model=List[FlagReview])
async def list_flag_reviews(submission_id: UUID, user_id: CurrentUser):
    """Reviewers' dismissals of automated compliance flags, on every version."""
    return await flag_review_service.list_for(submission_id, user_id)


@router.put("/{submission_id}/flags/{rule_id}", response_model=FlagReview)
async def dismiss_flag(submission_id: UUID, rule_id: UUID, data: FlagDismissRequest, user_id: CurrentUser):
    """Dismiss a flag on the version in review as a false alarm, with a reason. The score itself isn't changed."""
    return await flag_review_service.dismiss(submission_id, rule_id, user_id, data)


@router.delete("/{submission_id}/flags/{rule_id}", status_code=status.HTTP_204_NO_CONTENT)
async def restore_flag(submission_id: UUID, rule_id: UUID, user_id: CurrentUser):
    """Undo a dismissal on the version in review."""
    await flag_review_service.undo(submission_id, rule_id, user_id)


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

