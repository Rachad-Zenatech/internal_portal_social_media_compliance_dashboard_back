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
    exception_terms: List[str] = Field(
        default_factory=list,
        description="Phrases that cancel a match inside them, e.g. 'not combat proven' for 'combat proven', or "
                    "'army of volunteers' for the trigger 'army'",
    )
    disclaimer_triggers: List[str] = Field(
        default_factory=list,
        description="Phrases that make a required disclaimer necessary. Empty: the disclaimer is needed on all content the rule applies to",
    )
    allowed_examples: Optional[str] = Field(None, description="Examples of acceptable phrasing or imagery")
    prohibited_examples: Optional[str] = Field(None, description="Examples of non-compliant phrasing or imagery")
    remediation_guidance: Optional[str] = Field(None, description="Instructions on how to fix or review violations")
    applies_to: List[str] = Field(
        default_factory=lambda: ["video", "imagery", "caption", "ceo_post"],
        description="Parts of content this rule governs: video, imagery, caption (the post's text, always checked) and "
                    "ceo_post (Shaun's own posts). A rule runs when the content has at least one of them",
    )
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
    exception_terms: Optional[List[str]] = None
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


class RuleExampleResult(BaseModel):
    example: str
    # FLAG: a "not acceptable" example the rule should catch. CLEAR: an "acceptable" one it should leave alone.
    expected: Literal["FLAG", "CLEAR"]
    flagged: bool
    matched_terms: List[str] = Field(default_factory=list)
    ok: bool
    # Unquoted example text may describe imagery rather than give wording, so a miss may not be a real gap.
    quoted: bool = True


class RuleSelfTest(BaseModel):
    rule_id: UUID
    rule_number: str
    title: str
    # False when the rule has no phrases or triggers to test (e.g. imagery-only rules).
    testable: bool
    passed: bool
    results: List[RuleExampleResult] = Field(default_factory=list)


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
    # What the content is made of, matched against each rule's applies_to (video, imagery, caption, ceo_post).
    # Empty: worked out from content_type; when that says nothing either, every rule's applies_to matches.
    content_parts: List[str] = Field(default_factory=list)


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
    # Points this finding took off the score.
    penalty: int = 0
    # Set when every word it flagged was already counted for a stricter rule (the same words, counted once).
    counted_with: Optional[str] = None
    # The rule's exceptions, so the per-moment video flags skip the same excused wording.
    exception_terms: List[str] = Field(default_factory=list)


class ManualReviewRule(BaseModel):
    """An applicable rule the automated check can't test from text (it has no phrases or disclaimers to look
    for, e.g. image editing or image rights), so a reviewer has to confirm it."""
    rule_id: UUID
    rule_number: str
    title: str
    severity: str


class CheckSignals(BaseModel):
    """What the check read from the post's symbols: cashtags, hashtags, @mentions and links (see post_signals)."""
    cashtags: List[str] = Field(default_factory=list)
    hashtags: List[str] = Field(default_factory=list)
    mentions: List[str] = Field(default_factory=list)
    # Domains linked to, e.g. "army.mil".
    links: List[str] = Field(default_factory=list)
    # The mentions and domains above that belong to a government or military.
    government_references: List[str] = Field(default_factory=list)
    # Audiences the post reaches whatever it targets: a cashtag ($ZENA) is about the stock, so investors read it.
    # Their rules are checked too, and the sentiment check holds the post to their standard.
    implied_audiences: List[str] = Field(default_factory=list)
    # Worth a look but not a rule breach, so no points off (outside links, too many hashtags).
    notes: List[str] = Field(default_factory=list)


class ComplianceCheckResponse(BaseModel):
    is_compliant: bool
    traffic_light: str = "GREEN"  # GREEN, YELLOW, RED
    score: int = 100  # 0 to 100
    # A critical (red) finding blocks publication; the score is then capped so it can't look like a pass.
    blocked: bool = False
    score_cap: Optional[int] = None
    # The score before the cap: 100 minus every finding's penalty.
    raw_score: int = 100
    # The rulebook as of this check (when any rule last changed), to tell whether a saved check is out of date.
    rulebook_version: Optional[str] = None
    violations_count: int = 0
    violations: List[ComplianceViolation] = Field(default_factory=list)
    recommendations: List[str] = Field(default_factory=list)
    rules_checked: int = 0
    # Of rules_checked, the ones the text check can't actually test: a reviewer confirms these when approving.
    manual_review_rules: List[ManualReviewRule] = Field(default_factory=list)
    # The scope the rules were picked by: the audiences (target plus implied; empty when none is set, so every
    # audience's rules ran) and the content parts (empty when not known, so every rule's applies_to matched).
    audiences_checked: List[str] = Field(default_factory=list)
    content_parts: List[str] = Field(default_factory=list)
    # Missing on checks saved before signals were read.
    signals: Optional[CheckSignals] = None
