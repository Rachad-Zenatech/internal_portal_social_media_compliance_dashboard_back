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
    # For the viewer.
    can_submit: bool = False
    can_review: bool = False
    can_respond: bool = False


class ScriptDecisionRequest(BaseModel):
    """The reviewer's call on the script. text is their edited version; when it differs from what the creator sent,
    the script goes back to the creator to accept (it can't be approved as is)."""
    decision: Literal["APPROVE", "SEND_BACK"]
    text: Optional[str] = Field(None, max_length=MAX_SCRIPT_CHARS)
    note: Optional[str] = Field(None, max_length=4000)


class ScriptReviseRequest(BaseModel):
    text: str = Field(..., min_length=1, max_length=MAX_SCRIPT_CHARS)
    note: Optional[str] = Field(None, max_length=4000)


class ScriptCheckRequest(BaseModel):
    text: str = Field(..., min_length=1, max_length=MAX_SCRIPT_CHARS)


class ScriptQueueItem(BaseModel):
    """A script waiting for a reviewer, as the Content Screener's Scripts tab lists it."""
    id: UUID
    reference_code: str
    title: str
    platforms: List[str] = Field(default_factory=list)
    brand_code: Optional[str] = None
    audience: Optional[str] = None
    creator_name: Optional[str] = None
    created_by: Optional[UUID] = None
    version_number: int
    sent_at: datetime
    score: Optional[int] = None
    traffic_light: Optional[str] = None
    approval_needed_by: Optional[datetime] = None
    # IN_REVIEW, CHANGES_REQUESTED, EDITS_PROPOSED or APPROVED, and when the reviewer decided (None while waiting).
    script_status: str = "IN_REVIEW"
    decided_at: Optional[datetime] = None
    can_review: bool = False
