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
    # A platform content policy, kept up to date from the platform's policy pages; only super admins change it.
    is_policy: bool = False
    # Read from a source (e.g. NEWSROOM) and kept in sync with it; the article it came from.
    auto_source: Optional[str] = None
    source_url: Optional[str] = None
    updated_at: datetime
    updated_by_name: Optional[str] = None


class ContextSync(BaseModel):
    """A sync of facts from a source (the newsroom): running, or how the last one went."""
    id: UUID
    status: Literal["RUNNING", "DONE", "FAILED"]
    trigger: str
    started_at: datetime
    finished_at: Optional[datetime] = None
    found: int = 0
    added: int = 0
    updated: int = 0
    unchanged: int = 0
    skipped: int = 0
    failed: int = 0
    error: Optional[str] = None
    started_by_name: Optional[str] = None


class PolicyProposal(BaseModel):
    """An update to a platform content-policy norm, drafted after its official policy page changed."""
    id: UUID
    context_id: UUID
    norm_title: str
    platforms: List[str] = Field(default_factory=list)
    source_urls: List[str] = Field(default_factory=list)
    # What changed, in short points.
    changes: List[str] = Field(default_factory=list)
    current_body: Optional[str] = None
    current_do_examples: Optional[str] = None
    current_dont_examples: Optional[str] = None
    body: str
    do_examples: Optional[str] = None
    dont_examples: Optional[str] = None
    status: Literal["PENDING", "APPROVED", "REJECTED", "SUPERSEDED"]
    created_at: datetime


class PolicyPageStatus(BaseModel):
    """One official policy page a norm is kept up to date from, and whether it can be watched."""
    url: str
    norm_title: str
    fetched_at: Optional[datetime] = None
    changed_at: Optional[datetime] = None
    # Why it couldn't be read last time (e.g. it only shows text in a browser); None when it was read.
    error: Optional[str] = None
    # Read and stored, so a change to it will be spotted.
    watching: bool = False


class PolicyWatch(BaseModel):
    """How the weekly check of the policy pages went, and the drafted updates waiting for a decision."""
    last_check: Optional[ContextSync] = None
    proposals: List[PolicyProposal] = Field(default_factory=list)
    # How many pages are being watched (read and stored), out of len(pages).
    watched_pages: int = 0
    pages: List[PolicyPageStatus] = Field(default_factory=list)


class PolicyProposalApprove(BaseModel):
    """Approve a drafted update, optionally with the super admin's own edits to its wording."""
    body: Optional[str] = Field(None, min_length=1, max_length=2000)
    do_examples: Optional[str] = Field(None, max_length=1000)
    dont_examples: Optional[str] = Field(None, max_length=1000)
