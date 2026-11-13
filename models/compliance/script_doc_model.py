"""Script documents: scripts written from a template and reviewed live by several reviewers (see script_doc_service)."""

from datetime import datetime
from typing import Any, Dict, List, Literal, Optional
from uuid import UUID

from pydantic import BaseModel, Field

from models.compliance.script_batch_model import BatchDetails

DocStatus = Literal["DRAFT", "IN_REVIEW", "LEVEL2_REVIEW", "CHANGES_REQUESTED", "APPROVED"]
Capacity = Literal["EDIT", "VIEW"]
Vote = Literal["APPROVE", "CHANGES", "DONE"]

MAX_HTML_CHARS = 400_000


class TemplateRow(BaseModel):
    time: str
    visual: str
    voiceover: str
    onscreen: str = ""


class ScriptTemplate(BaseModel):
    key: str
    name: str
    storyline: str
    description: str
    duration: str = ""
    rows: List[TemplateRow] = Field(default_factory=list)


class DocCreate(BaseModel):
    title: str = Field(..., min_length=1, max_length=300)
    template_key: str = Field("blank", max_length=60)


class DocUpdate(BaseModel):
    title: Optional[str] = Field(None, min_length=1, max_length=300)
    # What every post made from the script shares (the same fields as an uploaded batch).
    details: Optional[BatchDetails] = None


class DocSnapshot(BaseModel):
    """The script as the live editor shows it, saved by an editor."""
    html: str = Field(..., max_length=MAX_HTML_CHARS)
    text: str = Field(..., max_length=MAX_HTML_CHARS)


class ReviewerPick(BaseModel):
    user_id: UUID
    capacity: Capacity = "EDIT"


class DocSubmit(BaseModel):
    # At least one is required: a script can't be sent to nobody.
    reviewers: List[ReviewerPick] = Field(default_factory=list, max_length=30)
    note: Optional[str] = Field(None, max_length=4000)
    html: Optional[str] = Field(None, max_length=MAX_HTML_CHARS)
    text: Optional[str] = Field(None, max_length=MAX_HTML_CHARS)


class CheckIn(BaseModel):
    vote: Vote
    note: Optional[str] = Field(None, max_length=4000)
    # An approving reviewer already asking for the Content Manager's level 2 approval.
    pass_to_manager: bool = False


class Finalize(BaseModel):
    """Once every reviewer has checked in: where the script goes next."""
    route: Literal["LEVEL2", "CREATOR"]
    note: Optional[str] = Field(None, max_length=4000)


class ManagerDecision(BaseModel):
    decision: Literal["APPROVE", "SEND_BACK"]
    note: Optional[str] = Field(None, max_length=4000)


class DocReviewer(BaseModel):
    user_id: UUID
    name: str
    capacity: Capacity
    can_approve: bool = False
    color: str
    checked_in: bool = False
    vote: Optional[Vote] = None
    note: Optional[str] = None
    checked_at: Optional[datetime] = None


class DocAccess(BaseModel):
    """What the viewer may do with this script right now."""
    role: Literal["CREATOR", "MANAGER", "REVIEWER", "SUGGESTER", "VIEWER"]
    can_edit: bool = False
    can_note: bool = False
    can_submit: bool = False
    can_check_in: bool = False
    can_finalize: bool = False
    can_decide: bool = False
    can_delete: bool = False


class DocEvent(BaseModel):
    id: int
    round: int
    user_name: Optional[str] = None
    kind: str
    detail: Dict[str, Any] = Field(default_factory=dict)
    created_at: datetime


class DocVersion(BaseModel):
    id: int
    round: int
    label: str
    body_html: str
    body_text: str
    created_by_name: Optional[str] = None
    created_at: datetime


class DocSummary(BaseModel):
    id: UUID
    title: str
    template_key: str
    status: DocStatus
    round: int
    creator_id: UUID
    creator_name: Optional[str] = None
    reviewer_names: List[str] = Field(default_factory=list)
    checked_in: int = 0
    required: int = 0
    # Waiting for the viewer: a script they review, a level 2 for a manager, changes for the creator.
    needs_you: bool = False
    updated_at: datetime
    created_at: datetime


class DocPost(BaseModel):
    """A post made from one script of an approved document."""
    position: int
    title: str
    submission_id: Optional[UUID] = None
    reference_code: Optional[str] = None


class DocPage(BaseModel):
    """One script (page) of the creator's document, for the page list beside the editor."""
    id: UUID
    title: str
    status: DocStatus
    position: int
    round: int = 0
    checked_in: int = 0
    required: int = 0


class DocSetSummary(BaseModel):
    """A creator's document in the hub: how its scripts stand."""
    id: UUID
    title: str
    first_script_id: UUID
    total: int
    draft: int = 0
    in_review: int = 0
    changes_requested: int = 0
    approved: int = 0
    needs_you: bool = False
    updated_at: datetime


class BulkSubmit(BaseModel):
    """Send several scripts of the document for review at once (all that can be sent, or the ones listed)."""
    reviewers: List[ReviewerPick] = Field(default_factory=list, max_length=30)
    note: Optional[str] = Field(None, max_length=4000)
    ids: Optional[List[UUID]] = Field(None, max_length=100)


class BulkFailure(BaseModel):
    id: UUID
    title: str
    reason: str


class BulkResult(BaseModel):
    sent: int
    failed: List[BulkFailure] = Field(default_factory=list)


class UploadedPages(BaseModel):
    created: List[DocPage] = Field(default_factory=list)


class DocDetail(BaseModel):
    id: UUID
    title: str
    template_key: str
    status: DocStatus
    round: int
    creator_id: UUID
    creator_name: Optional[str] = None
    creator_color: str
    body_html: str
    body_text: str
    details: Dict[str, Any] = Field(default_factory=dict)
    send_note: Optional[str] = None
    decision_note: Optional[str] = None
    decided_by_name: Optional[str] = None
    decided_at: Optional[datetime] = None
    reviewers: List[DocReviewer] = Field(default_factory=list)
    # What is still empty in the details, by name (as on an uploaded batch).
    missing_details: List[str] = Field(default_factory=list)
    # The creator's document this script is a page of, and its pages (shown to the creator and Content Managers).
    set_id: Optional[UUID] = None
    set_title: Optional[str] = None
    pages: List[DocPage] = Field(default_factory=list)
    # The post made from the script once it is approved.
    posts: List[DocPost] = Field(default_factory=list)
    # Set when the script is edited in the Share Document server's editor (shown in an iframe) instead of this portal's.
    share_document_id: Optional[str] = None
    all_checked: bool = False
    access: DocAccess
    # The viewer's own colour in the editor (their cursor and notes).
    my_color: str
    updated_at: datetime
    created_at: datetime


class DocHistory(BaseModel):
    events: List[DocEvent] = Field(default_factory=list)
    versions: List[DocVersion] = Field(default_factory=list)
