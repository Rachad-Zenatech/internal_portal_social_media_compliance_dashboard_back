"""Content Submission Pydantic Models
Drafts, file attachments, versions, review jobs and activity for the New Submission flow.
"""

from datetime import datetime
from typing import Any, Dict, List, Literal, Optional
from uuid import UUID

from pydantic import BaseModel, Field

ContentType = Literal["VIDEO", "SOCIAL_POST", "CAROUSEL", "IMAGE", "BLOG"]
FileKind = Literal["MAIN", "SCRIPT", "IMAGE", "SUPPORTING"]
SubmissionStatus = Literal["DRAFT", "IN_REVIEW", "CHANGES_REQUESTED", "APPROVED", "REJECTED"]

AUDIENCES: Dict[str, str] = {
    "GENERAL": "General Audience",
    "INVESTORS": "Investors",
    "DEFENSE": "Defense & Government",
    "COMMERCIAL": "Commercial Customers",
}
REGIONS: List[str] = ["Global", "United States", "Canada", "Europe", "Middle East", "Asia-Pacific"]
PLATFORMS: List[str] = ["TIKTOK", "YOUTUBE", "YOUTUBE_SHORTS", "LINKEDIN", "INSTAGRAM", "FACEBOOK", "X", "REDDIT", "WEBSITE"]
RULE_SETS: List[str] = ["Public Company Disclosure", "ZenaTech General", "ZenaDrone Defense", "Shaun CEO Thought Leadership"]
REVIEWER_ROUTES: List[str] = ["Marketing Review", "Compliance Review", "Legal Review", "Investor Relations Review", "Executive Review"]
MAX_ACCOUNTS = 20  # publishing accounts on one submission

# oauth_connections.provider -> the platform it publishes to. Paid-ads providers are not publishing accounts.
PROVIDER_PLATFORMS: Dict[str, str] = {
    "LINKEDIN_PAGE": "LINKEDIN",
    "LINKEDIN_PROFILE": "LINKEDIN",
    "LINKEDIN": "LINKEDIN",
    "X_TWITTER": "X",
    "TWITTER": "X",
    "X": "X",
    "YOUTUBE": "YOUTUBE",
    "FACEBOOK_PAGE": "FACEBOOK",
    "FACEBOOK": "FACEBOOK",
    "INSTAGRAM": "INSTAGRAM",
    "TIKTOK": "TIKTOK",
    "REDDIT": "REDDIT",
}


class BrandOption(BaseModel):
    code: str
    name: str


class AccountOption(BaseModel):
    id: UUID
    label: str
    account_name: str
    provider: str
    platform: str
    handle: Optional[str] = None
    brand_code: Optional[str] = None
    is_ceo: bool = False
    is_demo: bool = False


class SubmissionOptions(BaseModel):
    brands: List[BrandOption]
    accounts: List[AccountOption]
    audiences: Dict[str, str] = AUDIENCES
    regions: List[str] = REGIONS
    platforms: List[str] = PLATFORMS
    rule_sets: List[str] = RULE_SETS
    reviewer_routes: List[str] = REVIEWER_ROUTES


class SubmissionCreate(BaseModel):
    title: str = Field(..., min_length=1, max_length=300)
    content_type: ContentType


class SubmissionUpdate(BaseModel):
    title: Optional[str] = Field(None, min_length=1, max_length=300)
    content_type: Optional[ContentType] = None
    brand_code: Optional[str] = Field(None, max_length=50)
    audience: Optional[str] = Field(None, max_length=50)
    region: Optional[str] = Field(None, max_length=100)
    platforms: Optional[List[str]] = Field(None, max_length=len(PLATFORMS))
    # Every account it posts through, primary first. account_id alone still works and means just that one.
    account_ids: Optional[List[UUID]] = Field(None, max_length=MAX_ACCOUNTS)
    account_id: Optional[UUID] = None
    publish_at: Optional[datetime] = None
    caption_html: Optional[str] = Field(None, max_length=200_000)
    caption_text: Optional[str] = Field(None, max_length=100_000)
    rule_sets: Optional[List[str]] = Field(None, max_length=len(RULE_SETS))
    reviewer_route: Optional[List[str]] = Field(None, max_length=len(REVIEWER_ROUTES))
    # Explicitly clear nullable fields (e.g. account_id, publish_at) by listing them here. account_id clears every account.
    clear_fields: List[Literal["account_id", "publish_at", "brand_code", "audience", "region"]] = Field(default_factory=list)


TranscriptStatus = Literal["PENDING", "PROCESSING", "COMPLETED", "FAILED"]


class TranscriptSegment(BaseModel):
    start_sec: float
    end_sec: float
    text: str
    # "speech" (said aloud) or "on_screen" (text shown in the video). Older transcripts are speech only.
    source: Literal["speech", "on_screen"] = "speech"


class SubmissionFile(BaseModel):
    id: UUID
    kind: FileKind
    original_name: str
    mime_type: str
    size_bytes: int
    created_at: datetime
    url: str
    # Video files only: speech transcript generated in the background after upload.
    transcript_status: Optional[TranscriptStatus] = None
    transcript: Optional[List[TranscriptSegment]] = None
    transcript_error: Optional[str] = None


class SubmissionSummary(BaseModel):
    id: UUID
    reference_code: str
    title: str
    content_type: str
    brand_code: Optional[str] = None
    platforms: List[str] = Field(default_factory=list)
    status: str
    current_version: int
    publish_at: Optional[datetime] = None
    latest_traffic_light: Optional[str] = None
    latest_score: Optional[int] = None
    file_count: int = 0
    # Review comments across all versions, for the count on each Content Portal row.
    comment_count: int = 0
    submitted_at: Optional[datetime] = None
    created_at: datetime
    updated_at: datetime
    # For the Content Portal cards: where it posts, a caption preview and the file to use as the thumbnail.
    audience: Optional[str] = None
    # The primary account (first picked), and every account it posts through.
    account: Optional[AccountOption] = None
    accounts: List[AccountOption] = Field(default_factory=list)
    caption_text: Optional[str] = None
    cover: Optional[SubmissionFile] = None
    created_by: Optional[UUID] = None
    created_by_name: Optional[str] = None
    # Drafts only: the creator, super admins and Content Managers may delete them.
    can_delete: bool = False
    # In review and the viewer can review it (a reviewer, and not their own upload).
    can_review: bool = False


class Submission(BaseModel):
    id: UUID
    reference_code: str
    title: str
    content_type: str
    brand_code: Optional[str] = None
    audience: Optional[str] = None
    region: Optional[str] = None
    platforms: List[str] = Field(default_factory=list)
    # account_id / account are the primary (first) of account_ids / accounts.
    account_id: Optional[UUID] = None
    account: Optional[AccountOption] = None
    account_ids: List[UUID] = Field(default_factory=list)
    accounts: List[AccountOption] = Field(default_factory=list)
    publish_at: Optional[datetime] = None
    caption_html: Optional[str] = None
    caption_text: Optional[str] = None
    rule_sets: List[str] = Field(default_factory=list)
    reviewer_route: List[str] = Field(default_factory=list)
    status: str
    current_version: int
    latest_check: Optional[Dict[str, Any]] = None
    files: List[SubmissionFile] = Field(default_factory=list)
    can_edit: bool
    created_by: Optional[UUID] = None
    created_by_name: Optional[str] = None
    # For the person viewing it: drafts can be deleted by their creator, super admins and Content Managers.
    can_delete: bool = False
    # For the person viewing it: they're a reviewer and this is in review (and isn't their own upload).
    can_review: bool = False
    submitted_at: Optional[datetime] = None
    created_at: datetime
    updated_at: datetime


class SubmissionVersion(BaseModel):
    version_number: int
    snapshot: Dict[str, Any]
    created_by_name: Optional[str] = None
    created_at: datetime


class ReviewJob(BaseModel):
    id: UUID
    version_number: int
    status: str
    reviewer_route: List[str]
    traffic_light: Optional[str] = None
    score: Optional[int] = None
    automated_check: Optional[Dict[str, Any]] = None
    created_at: datetime
    completed_at: Optional[datetime] = None
    # The reviewer's call on this version.
    decision: Optional[str] = None
    decision_notes: Optional[str] = None
    decided_by_name: Optional[str] = None
    decided_at: Optional[datetime] = None


ReviewDecisionType = Literal["APPROVE", "REQUEST_CHANGES"]


class ReviewDecisionRequest(BaseModel):
    """A reviewer's decision on the version currently in review."""
    decision: ReviewDecisionType
    # Required when requesting changes (unless there are open comments on this version); shown to the creator.
    notes: Optional[str] = Field(None, max_length=5000)
    # Approve: schedule on the calendar for this time (defaults to the submission's planned publish date)...
    publish_at: Optional[datetime] = None
    # ...or approve to publish straight away.
    publish_now: bool = False


CommentCategory = Literal["LEGAL", "EDITORIAL", "COMPLIANCE", "BRAND", "NOTE"]


class ReviewCommentCreate(BaseModel):
    body: str = Field(..., min_length=1, max_length=5000)
    category: CommentCategory = "NOTE"
    start_sec: Optional[float] = Field(None, ge=0)
    end_sec: Optional[float] = Field(None, ge=0)


class ReviewCommentUpdate(BaseModel):
    resolved: bool


class ReviewComment(BaseModel):
    id: UUID
    submission_id: UUID
    version_number: int
    category: str
    body: str
    start_sec: Optional[float] = None
    end_sec: Optional[float] = None
    author_id: Optional[UUID] = None
    author_name: Optional[str] = None
    # Written by a reviewer (rather than the creator replying).
    from_reviewer: bool = False
    resolved_at: Optional[datetime] = None
    resolved_by_name: Optional[str] = None
    created_at: datetime
    can_delete: bool = False


class SubmissionEvent(BaseModel):
    id: int
    event_type: str
    message: str
    data: Optional[Dict[str, Any]] = None
    actor_name: Optional[str] = None
    created_at: datetime


class SubmissionHistory(BaseModel):
    versions: List[SubmissionVersion]
    review_jobs: List[ReviewJob]
    events: List[SubmissionEvent]


class ReviewQueueItem(BaseModel):
    """A submitted video, as the Content Screener lists it."""
    id: UUID
    reference_code: str
    title: str
    status: str
    platforms: List[str] = Field(default_factory=list)
    account: Optional[AccountOption] = None
    caption_text: Optional[str] = None
    latest_check: Optional[Dict[str, Any]] = None
    current_version: int
    submitted_at: Optional[datetime] = None
    creator_name: Optional[str] = None
    video: SubmissionFile
    can_review: bool = False
    publish_at: Optional[datetime] = None


class SubmitResult(BaseModel):
    submission: Submission
    review_job: ReviewJob
