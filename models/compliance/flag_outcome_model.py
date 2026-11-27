"""Read-only history of reviewers' flag decisions."""
from datetime import datetime
from typing import Literal, Optional
from uuid import UUID
from pydantic import BaseModel


class FlagOutcome(BaseModel):
    rule_id: UUID
    rule_number: str
    version_number: int
    outcome: Literal["DISMISSED", "APPROVED", "REJECTED"]
    reason: str
    note: Optional[str] = None
    reviewer_name: Optional[str] = None
    decided_at: Optional[datetime] = None
