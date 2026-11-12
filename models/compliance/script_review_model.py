"""Script stage: a video post's script is checked, reviewed (the reviewer may edit it) and approved before the video."""

from datetime import datetime
from typing import Any, Dict, List, Literal, Optional
from uuid import UUID

from pydantic import BaseModel, Field

ScriptStatus = Literal["DRAFT", "IN_REVIEW", "EDITS_PROPOSED", "CHANGES_REQUESTED", "APPROVED"]
ScriptSource = Literal["UPLOAD", "REVIEWER_EDIT", "CREATOR_EDIT"]

MAX_SCRIPT_CHARS = 50_000


class ScriptVersion(BaseModel):
    version_number: int
    body: str
    # UPLOAD: read from the creator's file. REVIEWER_EDIT / CREATOR_EDIT: edited in the platform.
    source: ScriptSource
    file_name: Optional[str] = None
    check: Optional[Dict[str, Any]] = None
    created_by: Optional[UUID] = None
    created_by_name: Optional[str] = None
    created_at: datetime
    # The reviewer's decision on this version, when it was one the creator sent: APPROVED, EDITED (they sent back
    # their own version) or CHANGES_REQUESTED.
    review_decision: Optional[str] = None
    review_note: Optional[str] = None
    reviewed_by_name: Optional[str] = None
    reviewed_at: Optional[datetime] = None


class ScriptSuggestion(BaseModel):
    """A suggesting reviewer's own suggestion on a script version: their edited wording and their notes on it."""
    version_number: int
    body: str
    highlights: List["ScriptHighlight"] = Field(default_factory=list)
    updated_at: datetime


class ScriptBatchRef(BaseModel):
    """Where this script sits in the batch it was uploaded with, for stepping through the batch one script at a time."""
    batch_id: UUID
    title: str
    # 1-based place in the batch, and how many scripts it has.
    position: int
    total: int
    # The posts of the scripts before and after this one (None at the ends).
    previous_id: Optional[UUID] = None
    next_id: Optional[UUID] = None
    # The next script after this one that is still waiting for a reviewer (going round the batch), if any.
    next_waiting_id: Optional[UUID] = None
    # How many of the batch's scripts are still waiting for a reviewer.
    waiting: int = 0


class ScriptStage(BaseModel):
    submission_id: UUID
    reference_code: str
    title: str
    # Whether this post goes through the script stage (video posts created since it existed).
    required: bool
    status: Optional[ScriptStatus] = None
    versions: List[ScriptVersion] = Field(default_factory=list)
    # The script file the creator uploaded, as read now (what "Send script for review" would send).
    file_name: Optional[str] = None
    file_status: Optional[str] = None
    file_error: Optional[str] = None
    file_text: Optional[str] = None
    file_url: Optional[str] = None
    # For the viewer.
    can_submit: bool = False
    can_review: bool = False
    can_respond: bool = False
    # The creator sent the script and it's still being read and scored (it reaches the reviewers when that's done).
    processing: bool = False
    # Why the last send failed (which step), so the creator can fix it and send again.
    send_error: Optional[str] = None
    # Set when the script came in a batch of scripts uploaded together.
    batch: Optional[ScriptBatchRef] = None
    # A suggesting reviewer (comments and suggested edits, no approving) looking at a script in review, and their own
    # suggestion so far.
    can_suggest: bool = False
    suggestion: Optional[ScriptSuggestion] = None
    # The creator's compliance check of the uploaded script, run before sending it; stale once the script, the post's
    # details or the rulebook changed since (then it's run again).
    precheck: Optional[Dict[str, Any]] = None
    precheck_stale: bool = False
    precheck_at: Optional[datetime] = None


class ScriptDecisionRequest(BaseModel):
    """The reviewer's call on the script. text is their edited version; when it differs from what the creator sent,
    the script goes back to the creator to accept (it can't be approved as is)."""
    decision: Literal["APPROVE", "SEND_BACK"]
    text: Optional[str] = Field(None, max_length=MAX_SCRIPT_CHARS)
    note: Optional[str] = Field(None, max_length=4000)
    # When the creator's next step is due: the changes if the script goes back, the video if it's approved. Working
    # days from now, or an exact date and time; sent back with neither it gets the normal window, approved with
    # neither the video has no deadline.
    due_days: Optional[Literal[1, 2, 3, 5]] = None
    due_at: Optional[datetime] = None


class ScriptReviseRequest(BaseModel):
    text: str = Field(..., min_length=1, max_length=MAX_SCRIPT_CHARS)
    note: Optional[str] = Field(None, max_length=4000)
    # The creator confirmed sending the same script again for another publish date.
    confirm_repost: bool = False


class ScriptSubmitRequest(BaseModel):
    # The creator confirmed sending the same script again for another publish date.
    confirm_repost: bool = False


class ScriptCheckRequest(BaseModel):
    text: str = Field(..., min_length=1, max_length=MAX_SCRIPT_CHARS)


class FlagFixRequest(BaseModel):
    """One flagged passage of a script and the rule it broke, to have a compliant rewrite suggested."""
    passage: str = Field(..., min_length=1, max_length=2000)
    flagged_terms: List[str] = Field(default_factory=list, max_length=20)
    rule_title: str = Field(..., min_length=1, max_length=300)
    explanation: Optional[str] = Field(None, max_length=2000)
    guidance: Optional[str] = Field(None, max_length=2000)


class FlagFixSuggestion(BaseModel):
    rewrite: str
    note: Optional[str] = None


class ScriptQueueItem(BaseModel):
    """A script waiting for a reviewer, as the Content Screener's Scripts tab lists it."""
    id: UUID
    reference_code: str
    title: str
    platforms: List[str] = Field(default_factory=list)
    brand_code: Optional[str] = None
    brand_codes: List[str] = Field(default_factory=list)
    audience: Optional[str] = None
    audiences: List[str] = Field(default_factory=list)
    creator_name: Optional[str] = None
    created_by: Optional[UUID] = None
    version_number: int
    sent_at: datetime
    score: Optional[int] = None
    # The AI review couldn't run for this script: no score, a message instead.
    ai_unavailable: bool = False
    traffic_light: Optional[str] = None
    approval_needed_by: Optional[datetime] = None
    # Set when the script came in a batch of scripts uploaded together (listed with that batch).
    batch_id: Optional[UUID] = None
    # IN_REVIEW, CHANGES_REQUESTED, EDITS_PROPOSED or APPROVED, and when the reviewer decided (None while waiting).
    script_status: str = "IN_REVIEW"
    decided_at: Optional[datetime] = None
    can_review: bool = False


class ScriptHighlight(BaseModel):
    """A stretch of the script the reviewer highlighted, with an optional note on it."""

    id: str = Field(min_length=1, max_length=40)
    # Where the wording started in the script as shown, and the wording itself (to find it again if the text moved).
    start: int = Field(ge=0)
    text: str = Field(min_length=1, max_length=5000)
    color: str = Field(pattern=r"^#[0-9a-fA-F]{6}$")
    note: str = Field(default="", max_length=2000)
    # Set by the creator when they send the next version: they fixed this, and the wording now reads `updated`
    # (None when the wording itself didn't change).
    done: bool = False
    updated: Optional[str] = Field(default=None, max_length=5000)


class ScriptResolution(BaseModel):
    id: str = Field(min_length=1, max_length=40)
    done: bool
    updated: Optional[str] = Field(default=None, max_length=5000)


class ScriptResolutionsRequest(BaseModel):
    resolutions: List[ScriptResolution] = Field(default_factory=list, max_length=500)


class ScriptSuggestionRequest(BaseModel):
    """What a suggesting reviewer saves: their edited wording and their notes on it."""
    text: str = Field(..., min_length=1, max_length=MAX_SCRIPT_CHARS)
    highlights: List[ScriptHighlight] = Field(default_factory=list, max_length=500)


class ScriptHighlightsRequest(BaseModel):
    highlights: List[ScriptHighlight] = Field(default_factory=list, max_length=500)


# ScriptSuggestion refers to ScriptHighlight, defined further down.
ScriptSuggestion.model_rebuild()
ScriptStage.model_rebuild()
