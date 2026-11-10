"""Social media content policies: the platform policy pages read, and the rules proposed from them for approval."""

from datetime import datetime
from typing import List, Literal, Optional
from uuid import UUID

from pydantic import BaseModel, Field

PageStatus = Literal["NEW", "READ", "NEEDS_TEXT", "FAILED"]
ProposalStatus = Literal["PENDING", "APPROVED", "REJECTED", "SUPERSEDED"]


class PolicySource(BaseModel):
    platform: str
    region: str
    policy_name: str


class PolicyPage(BaseModel):
    """One policy URL and every platform/region it stands for."""
    id: UUID
    url: str
    status: PageStatus
    error: Optional[str] = None
    fetch_method: Optional[str] = None
    last_fetched_at: Optional[datetime] = None
    last_changed_at: Optional[datetime] = None
    text_chars: int = 0
    proposal_status: Optional[str] = None
    proposal_error: Optional[str] = None
    sources: List[PolicySource] = Field(default_factory=list)
    pending_count: int = 0
    approved_count: int = 0


class SimilarRule(BaseModel):
    """An existing rule a proposal resembles. DUPLICATE: it already covers this. PARTIAL: it overlaps in part."""
    rule_id: Optional[UUID] = None
    rule_number: str
    title: str
    overlap: Literal["DUPLICATE", "PARTIAL"]
    reason: str = ""
    score: float = 0.0


class PolicyRuleProposal(BaseModel):
    id: UUID
    page_id: UUID
    url: str
    status: ProposalStatus
    title: str
    summary: str
    severity: str
    traffic_light: str
    evaluation_mode: Literal["RULE", "CONTEXT"]
    evaluation_guidance: Optional[str] = None
    prohibited_terms: List[str] = Field(default_factory=list)
    exception_terms: List[str] = Field(default_factory=list)
    required_disclaimers: List[str] = Field(default_factory=list)
    disclaimer_triggers: List[str] = Field(default_factory=list)
    remediation_guidance: Optional[str] = None
    platforms: List[str] = Field(default_factory=list)
    regions: List[str] = Field(default_factory=list)
    paid_ads_only: bool = False
    source_quote: Optional[str] = None
    similar_rules: List[SimilarRule] = Field(default_factory=list)
    rule_id: Optional[UUID] = None
    rule_number: Optional[str] = None
    review_note: Optional[str] = None
    reviewed_by_name: Optional[str] = None
    reviewed_at: Optional[datetime] = None
    created_at: datetime
    policy_names: List[str] = Field(default_factory=list)


class ProposalUpdate(BaseModel):
    """What a reviewer can change before approving."""
    title: Optional[str] = Field(None, min_length=1, max_length=255)
    summary: Optional[str] = Field(None, min_length=1)
    severity: Optional[Literal["CRITICAL", "HIGH", "MEDIUM", "LOW"]] = None
    traffic_light: Optional[Literal["RED", "YELLOW", "GREEN"]] = None
    evaluation_mode: Optional[Literal["RULE", "CONTEXT"]] = None
    evaluation_guidance: Optional[str] = None
    prohibited_terms: Optional[List[str]] = None
    exception_terms: Optional[List[str]] = None
    required_disclaimers: Optional[List[str]] = None
    disclaimer_triggers: Optional[List[str]] = None
    remediation_guidance: Optional[str] = None
    platforms: Optional[List[str]] = None
    regions: Optional[List[str]] = None
    paid_ads_only: Optional[bool] = None


class ProposalDecision(BaseModel):
    note: Optional[str] = Field(None, max_length=2000)


class PastedPolicyText(BaseModel):
    text: str = Field(..., min_length=200, max_length=400_000)


class PolicyOverview(BaseModel):
    rulebook_id: Optional[UUID] = None
    rulebook_title: str
    pages: List[PolicyPage] = Field(default_factory=list)
    pending: int = 0
    # A fetch-all run in progress.
    refreshing: bool = False
