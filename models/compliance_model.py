"""Compliance Rules & Rulebook Pydantic Models
Supports full CRUD ("build on it and remove") and live automated AI compliance screening.
"""

from datetime import datetime
from typing import List, Literal, Optional
from uuid import UUID
from pydantic import BaseModel, Field


class ComplianceRuleBase(BaseModel):
    rule_number: str = Field(..., description="Unique rule identifier e.g. 'Rule 1', 'Section 10'")
    category: str = Field(..., description="Category: Thought Leadership, Product Maturity, Defense Imagery, AI & Concepts, Security, etc.")
    title: str = Field(..., description="Concise rule title")
    summary: str = Field(..., description="Detailed description and policy standard")
    severity: str = Field("HIGH", description="Severity: CRITICAL, HIGH, MEDIUM, LOW")
    traffic_light: str = Field("YELLOW", description="Traffic light risk rating: RED, YELLOW, GREEN")
    prohibited_terms: List[str] = Field(default_factory=list, description="Keywords or phrases that trigger flags in text/captions/transcripts")
    required_disclaimers: List[str] = Field(default_factory=list, description="Mandatory disclaimers or concept labels required by this rule")
    disclaimer_triggers: List[str] = Field(
        default_factory=list,
        description="Phrases that make a required disclaimer necessary. Empty: the disclaimer is needed on all content the rule applies to",
    )
    allowed_examples: Optional[str] = Field(None, description="Examples of acceptable phrasing or imagery")
    prohibited_examples: Optional[str] = Field(None, description="Examples of non-compliant phrasing or imagery")
    remediation_guidance: Optional[str] = Field(None, description="Instructions on how to fix or review violations")
    applies_to: List[str] = Field(default_factory=lambda: ["video", "imagery", "caption", "ceo_post"], description="Content types this rule governs")
    # Scope: an empty list means the rule applies to every value of that dimension.
    rule_sets: List[str] = Field(default_factory=list, description="Rule sets this rule belongs to, e.g. 'ZenaDrone Defense'")
    brands: List[str] = Field(default_factory=list, description="Brand codes this rule is limited to")
    audiences: List[str] = Field(default_factory=list, description="Audience codes this rule is limited to")
    regions: List[str] = Field(default_factory=list, description="Regions this rule is limited to")
    platforms: List[str] = Field(default_factory=list, description="Platforms this rule is limited to")
    is_active: bool = Field(True, description="Whether this rule is actively enforced")
    sort_order: int = Field(0, description="Display order within category")


class ComplianceRuleCreate(ComplianceRuleBase):
    rulebook_id: Optional[UUID] = None


class ComplianceRuleUpdate(BaseModel):
    rule_number: Optional[str] = None
    category: Optional[str] = None
    title: Optional[str] = None
    summary: Optional[str] = None
    severity: Optional[str] = None
    traffic_light: Optional[str] = None
    prohibited_terms: Optional[List[str]] = None
    required_disclaimers: Optional[List[str]] = None
    disclaimer_triggers: Optional[List[str]] = None
    allowed_examples: Optional[str] = None
    prohibited_examples: Optional[str] = None
    remediation_guidance: Optional[str] = None
    applies_to: Optional[List[str]] = None
    rule_sets: Optional[List[str]] = None
    brands: Optional[List[str]] = None
    audiences: Optional[List[str]] = None
    regions: Optional[List[str]] = None
    platforms: Optional[List[str]] = None
    is_active: Optional[bool] = None
    sort_order: Optional[int] = None


class ComplianceRule(ComplianceRuleBase):
    id: UUID
    rulebook_id: Optional[UUID] = None
    is_custom: bool = False
    created_at: datetime
    updated_at: datetime
    created_by: Optional[UUID] = None
    updated_by: Optional[UUID] = None


class ComplianceCheckRequest(BaseModel):
    text: str = Field(..., description="Script, caption, claim, transcript, or video description to evaluate")
    video_title: Optional[str] = None
    content_type: Optional[str] = Field("video_caption", description="Type: video_caption, script, social_post, imagery_concept")
    # Who and where the content is for. Only rules whose scope matches are run; anything left empty
    # (e.g. no rule sets chosen) doesn't narrow the check.
    rule_sets: List[str] = Field(default_factory=list)
    brand_code: Optional[str] = None
    audience: Optional[str] = None
    region: Optional[str] = None
    platforms: List[str] = Field(default_factory=list)


class ComplianceViolation(BaseModel):
    rule_id: UUID
    rule_number: str
    title: str
    category: str
    severity: str
    traffic_light: str
    flagged_terms: List[str]
    explanation: str
    remediation_guidance: Optional[str] = None
    # PROHIBITED_TERM: wording the rule forbids. MISSING_DISCLAIMER: wording that needs a disclaimer, without it.
    kind: Literal["PROHIBITED_TERM", "MISSING_DISCLAIMER"] = "PROHIBITED_TERM"
    # Any one of these satisfies the rule.
    missing_disclaimers: List[str] = Field(default_factory=list)


class ComplianceCheckResponse(BaseModel):
    is_compliant: bool
    traffic_light: str = "GREEN"  # GREEN, YELLOW, RED
    score: int = 100  # 0 to 100
    violations_count: int = 0
    violations: List[ComplianceViolation] = Field(default_factory=list)
    recommendations: List[str] = Field(default_factory=list)
    rules_checked: int = 0
