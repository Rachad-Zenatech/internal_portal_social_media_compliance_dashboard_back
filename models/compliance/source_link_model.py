"""Models for submission source links and quick AI link checks."""

from typing import List, Optional
from pydantic import BaseModel, Field


class SourceLinksMixin(BaseModel):
    source_links: List[str] = Field(default_factory=list)


class SourceLinksUpdateMixin(BaseModel):
    source_links: Optional[List[str]] = Field(None, max_length=20)


class SourceLinkCheckRequest(BaseModel):
    url: str = Field(..., min_length=1, max_length=2000)

