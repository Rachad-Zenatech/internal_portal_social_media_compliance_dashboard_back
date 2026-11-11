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


class BatchItemCreate(BaseModel):
    title: str = Field(..., min_length=1, max_length=300)
    body: str = Field("", max_length=200_000)


class BatchItemUpdate(BaseModel):
    title: Optional[str] = Field(None, min_length=1, max_length=300)
    body: Optional[str] = Field(None, max_length=200_000)


class BatchItem(BaseModel):
    id: UUID
    position: int
    title: str
    body: str
    status: ItemStatus
    # The rulebook check of the script (score, flags), as for a post's script.
    check: Optional[Dict[str, Any]] = None
    review_note: Optional[str] = None
    reviewed_by_name: Optional[str] = None
    reviewed_at: Optional[datetime] = None
    # The post made from it once approved.
    submission_id: Optional[UUID] = None
    reference_code: Optional[str] = None


class Batch(BaseModel):
    id: UUID
    title: str
    source_name: Optional[str] = None
    status: BatchStatus
    created_by: UUID
    created_by_name: Optional[str] = None
    details: Dict[str, Any] = Field(default_factory=dict)
    # The details still missing before it can be sent, by their names on the form.
    missing_details: List[str] = Field(default_factory=list)
    items: List[BatchItem] = Field(default_factory=list)
    error: Optional[str] = None
    sent_at: Optional[datetime] = None
    created_at: datetime
    # What the viewer can do now.
    can_edit: bool = False
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
    # When each created post's video is due: working days from now, or an exact time. Neither: no deadline.
    due_days: Optional[int] = Field(None, ge=1, le=60)
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
