"""Content Submission Pydantic Models
Drafts, file attachments, versions, review jobs and activity for the New Submission flow.
"""

from datetime import datetime
from typing import Any, Dict, List, Literal, Optional
from uuid import UUID

from pydantic import BaseModel, Field

# AD: paid social ads. Only the type and its rule set exist so far; ad-specific fields and rules (claims,
# targeting, platform ad policies) are still to be defined.
ContentType = Literal["VIDEO", "SOCIAL_POST", "CAROUSEL", "IMAGE", "BLOG", "AD"]
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
RULE_SETS: List[str] = [
    "Public Company Disclosure", "ZenaTech General", "ZenaDrone Defense", "Shaun CEO Thought Leadership",
    # No rules yet: ad rules get added in the Compliance Rulebook once the requirements are agreed.
    "Paid Social Ads",
]
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
    # When the creator needs it approved by (required to submit); becomes the reviewer's deadline.
    approval_needed_by: Optional[datetime] = None
    caption_html: Optional[str] = Field(None, max_length=200_000)
    caption_text: Optional[str] = Field(None, max_length=100_000)
    rule_sets: Optional[List[str]] = Field(None, max_length=len(RULE_SETS))
    reviewer_route: Optional[List[str]] = Field(None, max_length=len(REVIEWER_ROUTES))
    # Explicitly clear nullable fields (e.g. account_id, publish_at) by listing them here. account_id clears every account.
    clear_fields: List[Literal["account_id", "publish_at", "approval_needed_by", "brand_code", "audience", "region"]] = Field(
        default_factory=list
    )


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
    # Where it came from. CANVA files remember the design and revision, so the creator can reimport the latest.
    source: Literal["UPLOAD", "CANVA"] = "UPLOAD"
    canva_design_id: Optional[str] = None
    canva_design_title: Optional[str] = None
    canva_revision: Optional[int] = None


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
    approval_needed_by: Optional[datetime] = None
    # When the next step is due: the reviewer's decision (REVIEW) or the creator's resubmission (REVISION).
    due_at: Optional[datetime] = None
    due_kind: Optional[Literal["REVIEW", "REVISION"]] = None
    latest_traffic_light: Optional[str] = None
    # None while the post is still being analysed (analysis_pending): no score until it's been read in full.
    latest_score: Optional[int] = None
    # How many rulebook issues the latest check found; None while it's still being analysed.
    issue_count: Optional[int] = None
    analysis_pending: bool = False
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
    # Final approvers named by an approval path for this brand/audience; empty means any brand reviewer.
    approver_names: List[str] = Field(default_factory=list)
    # The viewer may approve it (a reviewer, and one of its named approvers when it has any).
    can_approve: bool = False
    # Approved and the viewer is a reviewer who may reschedule it or reopen it for changes.
    can_manage_approval: bool = False


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
    # When the creator needs it approved by; the reviewer's deadline once it's submitted.
    approval_needed_by: Optional[datetime] = None
    # When the next step is due: the reviewer's decision (REVIEW) or the creator's resubmission (REVISION).
    due_at: Optional[datetime] = None
    due_kind: Optional[Literal["REVIEW", "REVISION"]] = None
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
    # Final approvers named by an approval path for this brand/audience; empty means any brand reviewer.
    approver_names: List[str] = Field(default_factory=list)
    # The viewer may approve it (a reviewer, and one of its named approvers when it has any).
    can_approve: bool = False
    # Critical (red) flags on the current version that a reviewer hasn't dismissed; approval waits on these.
    open_critical_flags: int = 0
    # The saved check was made before the rulebook last changed (new, edited or removed rules).
    check_outdated: bool = False
    # For the person viewing it: it's approved and they're a reviewer who may reschedule it or reopen it.
    can_manage_approval: bool = False
    submitted_at: Optional[datetime] = None
    created_at: datetime
    updated_at: datetime


class SubmissionVersion(BaseModel):
    version_number: int
    snapshot: Dict[str, Any]
    created_by_name: Optional[str] = None
    created_at: datetime


class VersionView(BaseModel):
    """One submitted version as it was: what was sent, its video, its automated check and its decision."""
    version_number: int
    latest_version: int
    snapshot: Dict[str, Any]
    created_at: datetime
    created_by_name: Optional[str] = None
    video: Optional["SubmissionFile"] = None
    check: Optional[Dict[str, Any]] = None
    review_job: Optional["ReviewJob"] = None


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
    # Approved: the rules the reviewer confirmed by hand ({rule_number, title}).
    manual_checks: List[Dict[str, Any]] = Field(default_factory=list)


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
    # Approve: the rule numbers the reviewer confirmed by hand. Must cover every rule the automated check lists
    # as needing a person (image editing, image rights, ...), since nothing else checks those.
    confirmed_rules: List[str] = Field(default_factory=list, max_length=100)
    # Request changes: working days the creator has to resubmit (1, 2, 3 or 5); the default window when left out.
    # Pulled in if the planned publish time is closer.
    revision_days: Optional[Literal[1, 2, 3, 5]] = None


class ReopenRequest(BaseModel):
    """A reviewer takes back an approval and sends the content to its creator with what to change."""
    notes: str = Field(..., min_length=1, max_length=5000)
    # Working days the creator has to resubmit; the default window when left out.
    revision_days: Optional[Literal[1, 2, 3, 5]] = None


class RescheduleRequest(BaseModel):
    """A reviewer moves approved content to a different publish time."""
    publish_at: datetime


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
    """A submitted post (video, image, carousel, text, ad or blog), as the Content Screener lists it."""
    id: UUID
    reference_code: str
    title: str
    content_type: Optional[str] = None
    status: str
    platforms: List[str] = Field(default_factory=list)
    account: Optional[AccountOption] = None
    caption_text: Optional[str] = None
    latest_check: Optional[Dict[str, Any]] = None
    current_version: int
    submitted_at: Optional[datetime] = None
    creator_name: Optional[str] = None
    # The post's video, when it has one.
    video: Optional[SubmissionFile] = None
    # Its images in order (main ones such as carousel slides first, then extras such as a cover).
    images: List[SubmissionFile] = Field(default_factory=list)
    can_review: bool = False
    # Final approvers named by an approval path for this brand/audience; empty means any brand reviewer.
    approver_names: List[str] = Field(default_factory=list)
    # The viewer may approve it (a reviewer, and one of its named approvers when it has any).
    can_approve: bool = False
    # Critical (red) flags on the current version that a reviewer hasn't dismissed; approval waits on these.
    open_critical_flags: int = 0
    publish_at: Optional[datetime] = None
    # When the next step is due: the reviewer's decision (REVIEW) or the creator's resubmission (REVISION).
    due_at: Optional[datetime] = None
    due_kind: Optional[Literal["REVIEW", "REVISION"]] = None
    created_by: Optional[UUID] = None
    # Approved content: the viewer is a reviewer who may reschedule it or reopen it for changes.
    can_manage_approval: bool = False
    # When the latest version was approved or sent back.
    decided_at: Optional[datetime] = None


QueueTab = Literal["review", "sent_back", "done"]


class ReviewQueueCounts(BaseModel):
    review: int = 0
    sent_back: int = 0
    done: int = 0


class DashboardActivity(BaseModel):
    """One recent thing that happened to content the viewer can see."""
    at: datetime
    actor_id: Optional[UUID] = None
    actor_name: Optional[str] = None
    # e.g. "approved", "requested changes on", "commented on"
    action: str
    submission_id: UUID
    reference_code: str
    title: str


class DashboardFinding(BaseModel):
    """A rulebook rule and how often it was flagged on content sent for review."""
    rule_number: str
    title: str
    count: int


class DashboardSummary(BaseModel):
    """The dashboard's activity feed and last-30-days compliance insights, over the content the viewer can see."""
    activity: List[DashboardActivity] = Field(default_factory=list)
    reviews_completed: int = 0
    approved: int = 0
    changes_requested: int = 0
    # Waiting on a reviewer past its review deadline (now, not over 30 days).
    overdue_reviews: int = 0
    # Average rulebook score of content sent for review in the period; None when nothing was sent.
    average_score: Optional[int] = None
    common_findings: List[DashboardFinding] = Field(default_factory=list)
    days: int = 30


class SubmitResult(BaseModel):
    submission: Submission
    review_job: ReviewJob
