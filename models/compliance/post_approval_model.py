"""The suggestion window following the first approval of a post."""
from datetime import datetime
from typing import Optional

from pydantic import BaseModel


class PostApprovalSuggestionFields(BaseModel):
    suggestions_until: Optional[datetime] = None
    can_suggest_after_approval: bool = False


class PublishingAccess(BaseModel):
    can_publish: bool
    suggestions_until: Optional[datetime] = None
    message: Optional[str] = None
