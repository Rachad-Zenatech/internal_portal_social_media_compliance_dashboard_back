"""Small queue records for review tab counts; no analysis is run to read them."""
from typing import Literal, Optional
from uuid import UUID
from pydantic import BaseModel


class ScriptQueueSummary(BaseModel):
    id: UUID
    script_status: Literal["IN_REVIEW", "CHANGES_REQUESTED", "EDITS_PROPOSED", "APPROVED"]
    batch_id: Optional[UUID] = None
