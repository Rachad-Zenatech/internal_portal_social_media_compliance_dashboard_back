"""Direct review-time caption edits and their immutable before/after history."""
from datetime import datetime
from typing import List, Optional
from uuid import UUID

from pydantic import BaseModel, Field, field_validator


class CaptionEditRequest(BaseModel):
    base_caption: str = Field(max_length=100_000)
    caption_text: str = Field(max_length=100_000)
    platform: Optional[str] = Field(default=None, max_length=40)

    @field_validator('caption_text')
    @classmethod
    def nonempty_caption(cls, value):
        if not value.strip():
            raise ValueError('Caption cannot be empty. Add wording before saving.')
        return value


class CaptionRevision(BaseModel):
    id: int
    before: str
    after: str
    platform: Optional[str] = None
    version: int
    actor_id: Optional[UUID] = None
    actor_name: str
    created_at: datetime


class CaptionReview(BaseModel):
    caption_html: Optional[str] = None
    caption_text: str
    platform_captions: dict[str, str]
    platforms: List[str]
    can_edit: bool
    needs_recheck: bool = False
    revisions: List[CaptionRevision]
