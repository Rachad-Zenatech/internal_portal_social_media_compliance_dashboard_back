"""Content Submission Router
Create, save and resume drafts, attach files, submit for compliance review and view history.
"""

import asyncio
import json
import logging
from typing import Annotated, Dict, List, Literal, Optional
from uuid import UUID

from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, UploadFile, status
from fastapi.responses import FileResponse, StreamingResponse

from models.compliance.canva_model import CanvaImportRequest, CanvaReimportRequest
from models.compliance.caption_model import CaptionSuggestions, CaptionSuggestRequest
from models.compliance.public_mood_model import CorrectionIn, MoodFeedbackIn, PublicMood, SentimentCorrectionIn, UnrelatedIn
from models.compliance.sentiment_model import FindingFeedback, FindingFeedbackIn, SentimentAnalysis
from models.compliance.submission_model import (
    DashboardSummary,
    Delegation,
    DelegationDirection,
    ReviewerOption,
    VersionView,
    Submission,
    SubmissionCreate,
    SubmissionFile,
    SubmissionHistory,
    SubmissionOptions,
    SubmissionSummary,
    SubmissionUpdate,
    SubmitResult,
    DuplicateReport,
    SubmitRequest,
)
from services.ai.observability import ai_observability, ai_usage_service
from services.compliance.submissions import duplicate_service
from services.compliance.media.file_mime_policy import safe_disposition_type
from services.compliance.media import caption_service
from services.compliance.analytics import conversation_evidence, conversation_feedback, public_mood_service
from services.compliance.analytics import sentiment_service
from services.compliance.submissions import submission_service as svc
from tools.compliance.submissions.review_queue_router import ViewScope, router as review_queue_router
from tools.compliance.submissions.submission_flag_router import (
    router as submission_flag_router,
    list_flag_reviews as list_flag_reviews,
    creator_flag_dismissals as creator_flag_dismissals,
    request_creator_flag_dismissal as request_creator_flag_dismissal,
    approve_creator_flag_dismissal as approve_creator_flag_dismissal,
    reject_creator_flag_dismissal as reject_creator_flag_dismissal,
    dismiss_flag as dismiss_flag,
    restore_flag as restore_flag,
    list_flag_verdicts as list_flag_verdicts,
    confirm_flag as confirm_flag,
    unconfirm_flag as unconfirm_flag,
)
from tools.compliance.submissions.submission_review_router import (
    router as submission_review_router,
    review_submission as review_submission,
    delegate_review as delegate_review,
    submission_delegations as submission_delegations,
    reopen_approval as reopen_approval,
    reschedule as reschedule,
    list_comments as list_comments,
    add_comment as add_comment,
    update_comment as update_comment,
    delete_comment as delete_comment,
)
from services.shared.auth.auth_service import get_current_user_id_dependency, require_permission
from services.compliance.media.hashtag_library import HashtagLibrary

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/submissions", tags=["Content Submissions"])
router.include_router(review_queue_router)
router.include_router(submission_flag_router)
router.include_router(submission_review_router)

CurrentUser = Annotated[UUID, Depends(get_current_user_id_dependency)]


@router.get("/options", response_model=SubmissionOptions)
async def get_options(user_id: CurrentUser):
    """Brands, connected publishing accounts and the allowed values for each field."""
    return await svc.get_options(user_id)


@router.get("/reviewers", response_model=List[ReviewerOption])
async def list_reviewers(user_id: CurrentUser, brand_code: Annotated[List[str], Query(max_length=20)] = []):
    """Reviewers and super admins a creator can send content to (on any of the brands, when given; repeat
    brand_code for a post on several brands)."""
    return await svc.reviewer_options(user_id, brand_code or None)


@router.get("/hashtags", response_model=HashtagLibrary)
async def get_hashtag_library(
    user_id: CurrentUser,
    brand_codes: Annotated[List[str], Query()] = [],
    audiences: Annotated[List[str], Query()] = [],
    region: Optional[str] = Query(None, max_length=50),
    platforms: Annotated[List[str], Query()] = [],
    rule_sets: Annotated[List[str], Query()] = [],
):
    """Hashtags (and the company cashtag) suited to this brand, audience and these platforms, minus any the
    rules that apply would flag, and how many the strictest platform suits. Drafts can call it before they're saved."""
    return await caption_service.get_hashtag_library(brand_codes[:10], audiences[:10], region, platforms[:20], rule_sets[:10])


@router.get("/delegations", response_model=List[Delegation])
async def list_delegations(user_id: CurrentUser, direction: DelegationDirection = Query("all")):
    """Review hand-offs the user made (by_me) or received (to_me), with each post's current status."""
    return await delegation_service.list_delegations(user_id, direction)


@router.get("/delegations/count")
async def delegation_count(user_id: CurrentUser):
    """Posts delegated to the user that are still waiting for a decision."""
    return {"pending": await delegation_service.delegation_count(user_id)}


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
    return FileResponse(path, media_type=mime, filename=name, content_disposition_type=safe_disposition_type(mime))


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
async def run_initial_check(submission_id: UUID, user_id: CurrentUser, body: Optional[SubmitRequest] = None):
    """Screen the title and caption against the rulebook without submitting. Duplicates are checked first: 409
    DUPLICATE_POST, or 409 REPOST_CONFIRM until confirm_repost is sent when only the publish date differs."""
    return await svc.run_initial_check(submission_id, user_id, confirm_repost=bool(body and body.confirm_repost))


@router.post("/{submission_id}/check/stream")
async def run_initial_check_stream(submission_id: UUID, user_id: CurrentUser, body: Optional[SubmitRequest] = None):
    """The same check as /check, streamed as newline-delimited JSON so the page can show its progress: an
    {"event": "attempt"} line as each AI model call starts and ends (fallbacks included), then {"event": "done",
    "submission": ...} with the saved result, or {"event": "failed", "status": ..., "detail": ...}."""
    await svc._get_owned_row(submission_id, user_id)  # access check before the stream starts
    queue: asyncio.Queue = asyncio.Queue()
    open_ = {"value": True}

    def listen(event: Dict) -> None:
        # Background work the check starts (reading an image's text) keeps the listener after the check is done.
        if open_["value"]:
            queue.put_nowait(event)

    async def work() -> None:
        ai_observability.attempt_listener.set(listen)
        try:
            sub = await svc.run_initial_check(submission_id, user_id, confirm_repost=bool(body and body.confirm_repost), refresh_sources=bool(body and body.refresh_sources))
            queue.put_nowait({"event": "done", "submission": sub.model_dump(mode="json")})
        except HTTPException as e:
            queue.put_nowait({"event": "failed", "status": e.status_code, "detail": e.detail})
        except Exception:
            logger.exception("Streamed compliance check failed for %s", submission_id)
            queue.put_nowait({"event": "failed", "status": 500, "detail": "The compliance check couldn't run"})

    # Runs to the end and saves its result even if the page goes away mid-check.
    task = asyncio.create_task(work(), name=f"check-stream-{submission_id}")

    async def lines():
        try:
            while True:
                event = await queue.get()
                yield json.dumps(event, default=str) + "\n"
                if event["event"] in ("done", "failed"):
                    break
        finally:
            open_["value"] = False
            if task.done() and not task.cancelled():
                task.exception()  # already handled inside work(); read so asyncio doesn't warn

    return StreamingResponse(lines(), media_type="application/x-ndjson", headers={"Cache-Control": "no-cache"})


@router.get("/{submission_id}/context-suggestions", response_model=Dict[str, List[str]])
async def context_suggestions(submission_id: UUID, user_id: CurrentUser):
    """The Messaging Context the analysis would pick on its own for this post, by kind (the picker's suggestions)."""
    from services.compliance.sentiment.input_builder import suggested_context

    await svc._get_owned_row(submission_id, user_id, manager_can_read=True)
    return await suggested_context(submission_id)


@router.get("/{submission_id}/duplicates", response_model=DuplicateReport)
async def find_duplicates(submission_id: UUID, user_id: CurrentUser):
    """Posts already sent for approval with the same content and caption as this draft, with how their Submission
    Details overlap. Sending is refused while one overlaps on every detail and the publish date. It is looked up as soon
    as the details are filled in, not only when the post is sent. A video post that is still at its script stage is
    compared by its script (the uploaded one, once read)."""
    row = await svc._get_owned_row(submission_id, user_id)
    if svc.script_stage_on(row) and row.get("script_status") != "APPROVED":
        from services.compliance.evaluation import script_review_service

        body = await script_review_service.current_script_text(row)
        return await duplicate_service.find_script(row, body) if body else DuplicateReport()
    return await duplicate_service.find(row)


@router.post("/{submission_id}/submit", response_model=SubmitResult)
async def submit_for_review(submission_id: UUID, user_id: CurrentUser, body: Optional[SubmitRequest] = None):
    """Validate required information, snapshot a new version and create a review job. A post that repeats one already
    sent for approval is refused (409 DUPLICATE_POST), or needs confirm_repost when only the date differs."""
    submission, job = await svc.submit_for_review(submission_id, user_id,
                                                  confirm_repost=bool(body and body.confirm_repost))
    return SubmitResult(submission=submission, review_job=job)


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
    await svc._get_owned_row(submission_id, user_id, manager_can_read=True)  # access check
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
    await svc.manual_check_service.require_sentiment_ready(submission_id, user_id)
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
    await svc.require_details_complete_for(submission_id, user_id, "reading the public mood")
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


@router.put("/{submission_id}/public-mood/conversations/{assessment_id}/items/{item_id}/unrelated",
            response_model=PublicMood)
async def mark_conversation_unrelated(submission_id: UUID, assessment_id: UUID, item_id: UUID, data: UnrelatedIn,
                                      user_id: CurrentUser):
    """This retrieved item is not about the post: it is left out of this post's conversation assessments from now on.
    The reason is saved against the assessment the reviewer was looking at (see GET /listening/feedback-reasons)."""
    await svc.get_submission(submission_id, user_id)  # access check
    return await public_mood_service.mark_conversation_unrelated(submission_id, assessment_id, item_id, user_id, data)


@router.delete("/{submission_id}/public-mood/conversations/items/{item_id}/unrelated", response_model=PublicMood)
async def restore_conversation_item(submission_id: UUID, item_id: UUID, user_id: CurrentUser):
    """Put a left-out item back."""
    await svc.get_submission(submission_id, user_id)  # access check
    return await public_mood_service.restore_conversation_item(submission_id, item_id, user_id)


@router.put("/{submission_id}/public-mood/conversations/{assessment_id}/correction", response_model=PublicMood)
async def correct_conversation_recommendation(submission_id: UUID, assessment_id: UUID, data: CorrectionIn,
                                              user_id: CurrentUser):
    """This recommendation is wrong: what it should have been and why. Recorded beside the assessment, one per person,
    changeable; the assessment itself stays as it was (public mood is advisory)."""
    await svc.get_submission(submission_id, user_id)  # access check
    return await public_mood_service.correct_conversation_recommendation(submission_id, assessment_id, user_id, data)


@router.put("/{submission_id}/public-mood/conversations/{assessment_id}/confirmation", response_model=PublicMood)
async def confirm_conversation_recommendation(submission_id: UUID, assessment_id: UUID, user_id: CurrentUser):
    """This recommendation is right. Recorded as the reviewer's decision, so the system also learns from agreement."""
    await svc.get_submission(submission_id, user_id)  # access check
    return await public_mood_service.confirm_conversation_recommendation(submission_id, assessment_id, user_id)


@router.put("/{submission_id}/public-mood/conversations/{assessment_id}/resolution", response_model=PublicMood)
async def resolve_conversation_decision(submission_id: UUID, assessment_id: UUID, data: CorrectionIn,
                                        user_id: Annotated[UUID, Depends(require_permission("CONFIG_ROLES_UPDATE"))]):
    """A lead reviewer settles a disagreement between reviewers about an assessment: until then an assessment whose
    reviewers disagree is left out of training. Needs the same permission as tuning the listening topics."""
    await svc.get_submission(submission_id, user_id)  # access check
    return await public_mood_service.resolve_conversation_decision(submission_id, assessment_id, user_id, data)


@router.put("/{submission_id}/public-mood/conversations/{assessment_id}/items/{item_id}/sentiment",
            response_model=PublicMood)
async def mark_conversation_sentiment(submission_id: UUID, assessment_id: UUID, item_id: UUID,
                                      data: SentimentCorrectionIn, user_id: CurrentUser):
    """This item's sentiment is read wrongly: what it should be. Recorded to improve the sentiment analysis."""
    await svc.get_submission(submission_id, user_id)  # access check
    return await public_mood_service.mark_conversation_sentiment(submission_id, assessment_id, item_id, user_id, data)


@router.delete("/{submission_id}/public-mood/conversations/{assessment_id}/items/{item_id}/sentiment",
               response_model=PublicMood)
async def clear_conversation_sentiment(submission_id: UUID, assessment_id: UUID, item_id: UUID, user_id: CurrentUser):
    await svc.get_submission(submission_id, user_id)  # access check
    return await public_mood_service.clear_conversation_sentiment(submission_id, assessment_id, item_id, user_id)


@router.delete("/{submission_id}/public-mood/conversations/{assessment_id}/correction", response_model=PublicMood)
async def clear_conversation_correction(submission_id: UUID, assessment_id: UUID, user_id: CurrentUser):
    await svc.get_submission(submission_id, user_id)  # access check
    return await public_mood_service.clear_conversation_correction(submission_id, assessment_id, user_id)


@router.get("/{submission_id}/public-mood/conversations/{assessment_id}/evidence")
async def conversation_evidence_of(submission_id: UUID, assessment_id: UUID, user_id: CurrentUser):
    """Every item an assessment looked at, as it read when the assessment ran (even if the feed has changed since),
    with the verdict on each."""
    await svc.get_submission(submission_id, user_id)  # access check
    await conversation_feedback.assessment_of(submission_id, assessment_id)  # 404 unless it belongs to this post
    return await conversation_evidence.assessment_evidence(assessment_id)


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
