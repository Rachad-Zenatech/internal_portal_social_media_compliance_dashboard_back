"""Activity events returned with submission history."""
from datetime import datetime
from typing import Any, Dict, Optional
from pydantic import BaseModel


class SubmissionEvent(BaseModel):
    id: int
    event_type: str
    message: str
    data: Optional[Dict[str, Any]] = None
    actor_name: Optional[str] = None
    created_at: datetime
