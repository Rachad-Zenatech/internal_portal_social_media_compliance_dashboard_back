"""Messaging Context: reference material the content analysis is given."""

from datetime import date, datetime
from typing import List, Literal, Optional
from uuid import UUID

from pydantic import BaseModel, Field

ContextKind = Literal["BRAND_VOICE", "FACT", "PLATFORM_NORM", "AUDIENCE_PROFILE"]


class ContextEntryIn(BaseModel):
    kind: ContextKind
    title: str = Field(..., min_length=1, max_length=200)
    body: str = Field(..., min_length=1, max_length=2000)
    do_examples: Optional[str] = Field(None, max_length=1000)
    dont_examples: Optional[str] = Field(None, max_length=1000)
    # Empty: applies to every brand / audience / platform.
    brands: List[str] = Field(default_factory=list)
    audiences: List[str] = Field(default_factory=list)
    platforms: List[str] = Field(default_factory=list)
    source: Optional[str] = Field(None, max_length=300)
    as_of: Optional[date] = None
    is_active: bool = True


class ContextEntry(ContextEntryIn):
    id: UUID
    is_starter: bool = False
    updated_at: datetime
    updated_by_name: Optional[str] = None
