"""Reviewers' attributed edits to a text-only post."""
from datetime import datetime
from typing import List, Optional
from uuid import UUID

from pydantic import BaseModel, Field, field_validator


class PostTextSave(BaseModel):
    text: str = Field(..., min_length=1, max_length=100_000)
    base_text: str = Field(..., max_length=100_000)
    base_revision: Optional[str] = None
    html: Optional[str] = Field(default=None, max_length=400_000)
    base_html: Optional[str] = Field(default=None, max_length=400_000)

    @field_validator("text")
    @classmethod
    def nonempty_text(cls, value):
        if not value.strip():
            raise ValueError("Post text cannot be empty")
        return value


class PostTextEdit(BaseModel):
    id: str
    reviewer_id: Optional[UUID] = None
    reviewer_name: str
    created_at: datetime
    version: int
    before_text: str
    after_text: str
    before_html: Optional[str] = None
    after_html: Optional[str] = None


class PostTextReview(BaseModel):
    text: str
    html: Optional[str] = None
    revision: Optional[str] = None
    can_edit: bool
    edits: List[PostTextEdit] = Field(default_factory=list)
