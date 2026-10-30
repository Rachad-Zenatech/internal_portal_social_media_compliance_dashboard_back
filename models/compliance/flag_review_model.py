"""Reviewers' calls on automated compliance flags: dismissing a false alarm, with a reason."""

from datetime import datetime
from typing import List, Literal, Optional
from uuid import UUID

from pydantic import BaseModel, Field

FlagDismissReason = Literal["NEGATED", "DIFFERENT_MEANING", "ACCEPTABLE_IN_CONTEXT", "RULE_TOO_BROAD", "OTHER"]

REASON_LABELS = {
    "NEGATED": "Says the opposite (negated)",
    "DIFFERENT_MEANING": "Word used in a different sense",
    "ACCEPTABLE_IN_CONTEXT": "Acceptable in this context",
    "RULE_TOO_BROAD": "The rule's phrase is too broad",
    "OTHER": "Other",
}


class FlagDismissRequest(BaseModel):
    reason: FlagDismissReason
    note: Optional[str] = Field(None, max_length=500)


class FlagReview(BaseModel):
    rule_id: UUID
    rule_number: str
    version_number: int
    flagged_terms: List[str] = Field(default_factory=list)
    traffic_light: str
    reason: FlagDismissReason
    reason_label: str
    note: Optional[str] = None
    decided_by_name: Optional[str] = None
    decided_at: datetime


class RuleFlagStats(BaseModel):
    rule_number: str
    # Submitted versions this rule flagged, and how many of those flags reviewers dismissed.
    flagged: int = 0
    dismissed: int = 0
    dismissal_rate: Optional[float] = None
    top_reason: Optional[str] = None
    top_reason_label: Optional[str] = None
