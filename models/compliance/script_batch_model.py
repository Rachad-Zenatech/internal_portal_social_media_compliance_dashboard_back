"""Script batches: several scripts uploaded in one document, reviewed together, each becoming a post once approved."""

from datetime import datetime
from typing import Any, Dict, List, Literal, Optional
from uuid import UUID

from pydantic import BaseModel, Field

BatchStatus = Literal["DRAFT", "PROCESSING", "IN_REVIEW", "CHANGES_REQUESTED", "DONE"]
ItemStatus = Literal["DRAFT", "IN_REVIEW", "CHANGES_REQUESTED", "APPROVED"]


class BatchDetails(BaseModel):
    """What every post made from the batch shares. The same fields as a post's details."""
    brand_codes: Optional[List[str]] = Field(None, max_length=20)
    audiences: Optional[List[str]] = Field(None, max_length=20)
    region: Optional[str] = Field(None, max_length=100)
    platforms: Optional[List[str]] = Field(None, max_length=20)
    account_ids: Optional[List[UUID]] = Field(None, max_length=20)
    rule_sets: Optional[List[str]] = Field(None, max_length=20)
    # When the creator needs the scripts approved by; it becomes the reviewers' deadline on each post.
    approval_needed_by: Optional[datetime] = None
    # The reviewers to send the batch to; empty sends it to every reviewer on its brand.
    reviewer_ids: Optional[List[UUID]] = Field(None, max_length=50)


class BatchUpdate(BaseModel):
    title: Optional[str] = Field(None, min_length=1, max_length=300)
    details: Optional[BatchDetails] = None
    # True: each script has its own details (before the batch is first sent). False: one set for the whole batch.
    per_script_details: Optional[bool] = None


class BatchItemCreate(BaseModel):
    title: str = Field(..., min_length=1, max_length=300)
    body: str = Field("", max_length=200_000)


class BatchItemUpdate(BaseModel):
    title: Optional[str] = Field(None, min_length=1, max_length=300)
    body: Optional[str] = Field(None, max_length=200_000)
    # This script's own details, when the batch sets them per script.
    details: Optional[BatchDetails] = None


class BatchItem(BaseModel):
    id: UUID
    position: int
    title: str
    body: str
    status: ItemStatus
    # The rulebook check of the script (score, flags), as for a post's script. Before sending, the creator's own check;
    # stale once its wording, its details or the rulebook changed since.
    check: Optional[Dict[str, Any]] = None
    check_stale: bool = False
    review_note: Optional[str] = None
    reviewed_by_name: Optional[str] = None
    reviewed_at: Optional[datetime] = None
    # The post made for it when the batch was sent; its script is reviewed on that post's own page.
    submission_id: Optional[UUID] = None
    reference_code: Optional[str] = None
    # The details its post gets: its own when the batch sets them per script, else the batch's. What is still missing
    # (per-script batches only; a shared set's gaps are on the batch).
    details: Dict[str, Any] = Field(default_factory=dict)
    missing_details: List[str] = Field(default_factory=list)


class Batch(BaseModel):
    id: UUID
    title: str
    source_name: Optional[str] = None
    status: BatchStatus
    created_by: UUID
    created_by_name: Optional[str] = None
    details: Dict[str, Any] = Field(default_factory=dict)
    # One set of details for every script (False), or each script has its own (True).
    per_script_details: bool = False
    # The details still missing before it can be sent, by their names on the form (the shared set; per script, see items).
    missing_details: List[str] = Field(default_factory=list)
    items: List[BatchItem] = Field(default_factory=list)
    error: Optional[str] = None
    sent_at: Optional[datetime] = None
    created_at: datetime
    # What the viewer can do now. can_edit: the details form (until the batch is first sent). can_change_scripts: edit,
    # add or remove scripts that haven't been sent yet.
    can_edit: bool = False
    can_change_scripts: bool = False
    can_send: bool = False
    can_review: bool = False


class BatchSummary(BaseModel):
    id: UUID
    title: str
    status: BatchStatus
    created_by: UUID
    created_by_name: Optional[str] = None
    total: int = 0
    in_review: int = 0
    approved: int = 0
    changes_requested: int = 0
    sent_at: Optional[datetime] = None
    created_at: datetime


class ItemDecision(BaseModel):
    item_id: UUID
    decision: Literal["APPROVE", "SEND_BACK"]
    # Why it goes back; required for SEND_BACK.
    note: Optional[str] = Field(None, max_length=5000)


class BatchReviewRequest(BaseModel):
    decisions: List[ItemDecision] = Field(..., min_length=1, max_length=200)
    # When each approved script's video is due: working days from now, or an exact time. Neither: no deadline.
    due_days: Optional[Literal[1, 2, 3, 5]] = None
    due_at: Optional[datetime] = None


class ItemResult(BaseModel):
    item_id: UUID
    ok: bool
    # What happened: APPROVED (a post was made), SENT_BACK, or why it was skipped.
    outcome: str
    submission_id: Optional[UUID] = None
    reference_code: Optional[str] = None


class BatchReviewResult(BaseModel):
    batch: Batch
    results: List[ItemResult]
