"""Duplicate-post comparisons shown before a creator submits a post."""
from datetime import datetime
from typing import List, Literal, Optional
from uuid import UUID

from pydantic import BaseModel, Field


class DuplicateDetail(BaseModel):
    """One Submission Details dropdown, compared with an earlier post that has the same content and caption."""
    field: Literal["brands", "accounts", "audiences", "region", "platforms"]
    label: str
    # Values both posts have.
    shared: List[str] = Field(default_factory=list)
    # Values only this post has.
    new: List[str] = Field(default_factory=list)


class DuplicateMatch(BaseModel):
    """A post already sent for approval with the same content and caption."""
    submission_id: UUID
    reference_code: str
    title: str
    status: str
    submitted_at: Optional[datetime] = None
    publish_at: Optional[datetime] = None
    created_by_name: Optional[str] = None
    details: List[DuplicateDetail] = Field(default_factory=list)
    # Every dropdown shares a value: the same post would go out to the same place again.
    all_overlap: bool = False
    same_date: bool = False


class DuplicateReport(BaseModel):
    """Earlier posts with the same content and caption. blocked: one overlaps on every detail and the publish date,
    so this can't be submitted. repost: one overlaps on every detail on another date, so the creator confirms."""
    matches: List[DuplicateMatch] = Field(default_factory=list)
    blocked: bool = False
    blocking: Optional[DuplicateMatch] = None
    repost: Optional[DuplicateMatch] = None
