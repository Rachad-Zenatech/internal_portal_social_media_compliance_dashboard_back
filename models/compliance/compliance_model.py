"""Compliance Rules & Rulebook Pydantic Models
Supports full CRUD ("build on it and remove") and live automated AI compliance screening.
"""

from datetime import datetime
from typing import List, Literal, Optional
from uuid import UUID
from pydantic import BaseModel, Field, model_validator


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
    evaluation_mode: Literal["RULE", "CONTEXT"] = Field(
        "RULE",
        description="RULE: a match is a finding, every time (hard bans and required disclaimers). CONTEXT: a match is "
                    "only a candidate, and the AI judges from the surrounding wording whether the rule really applies",
    )
    evaluation_guidance: Optional[str] = Field(
        None,
        description="For CONTEXT rules: what the judge must decide, e.g. who the statement is about, whether it is "
                    "history or opinion, and what would make it a violation, need review or need evidence",
    )


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
    evaluation_mode: Optional[Literal["RULE", "CONTEXT"]] = None
    evaluation_guidance: Optional[str] = None


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


class ProvidedSource(BaseModel):
    """A document the author attached as evidence (a supporting file), already read as text, to check claims against."""
    name: str
    text: str


class ComplianceCheckRequest(BaseModel):
    text: str = Field(..., description="Script, caption, claim, transcript, or video description to evaluate")
    video_title: Optional[str] = None
    content_type: Optional[str] = Field("video_caption", description="Type: video_caption, script, social_post, imagery_concept")
    # Who and where the content is for. Only rules whose scope matches are run; anything left empty
    # (e.g. no rule sets chosen) doesn't narrow the check.
    rule_sets: List[str] = Field(default_factory=list)
    # The brand(s) it's for. brand_code is the first of brand_codes; either one alone is enough.
    brand_code: Optional[str] = None
    brand_codes: List[str] = Field(default_factory=list)
    # The target audience(s). audience is the first of audiences; either one alone is enough.
    audience: Optional[str] = None
    audiences: List[str] = Field(default_factory=list)
    region: Optional[str] = None
    platforms: List[str] = Field(default_factory=list)
    # What the content is made of, matched against each rule's applies_to (video, imagery, caption, ceo_post).
    # Empty: worked out from content_type; when that says nothing either, every rule's applies_to matches.
    content_parts: List[str] = Field(default_factory=list)
    # Documents attached as evidence, read as text: what the post's claims are checked against, together with the links
    # the post itself cites (see source_check_service).
    sources: List[ProvidedSource] = Field(default_factory=list)
    # The parts of the content that get published: where a required disclaimer has to be. Empty or unset: a disclaimer
    # anywhere in `text` counts. Set, so a disclaimer in the title or an unpublished script doesn't stand in for one
    # the post itself doesn't carry.
    disclaimer_text: Optional[str] = None

    @model_validator(mode="after")
    def _keep_audience_and_audiences_in_step(self):
        merged = list(dict.fromkeys([*([self.audience] if self.audience else []), *self.audiences]))
        self.audiences = merged
        self.audience = merged[0] if merged else None
        brands = list(dict.fromkeys([*([self.brand_code] if self.brand_code else []), *self.brand_codes]))
        self.brand_codes = brands
        self.brand_code = brands[0] if brands else None
        return self


# What a finding amounts to once its context is read:
#   VIOLATION          the content breaks the rule (costs points; a critical one blocks publication)
#   NEEDS_REVIEW       whether it applies depends on something the text can't show, such as the final footage
#   EVIDENCE_REQUIRED  a factual claim that needs a source before it goes out
#   CLEARED            a trigger was found, but the context shows the rule doesn't apply
# Only violations block. Violations cost points; so do claims that need a source and checks the AI thinks are likely
# breaches (see REVIEW_SURE in compliance_service). Cleared triggers cost nothing.
FindingStatus = Literal["VIOLATION", "NEEDS_REVIEW", "EVIDENCE_REQUIRED", "CLEARED"]


class ContextEvaluation(BaseModel):
    """How the AI read a rule's trigger in context, kept beside the finding so the decision can be audited."""
    decision: FindingStatus
    # The passage the decision is about, exactly as it appears in the content.
    matched_text: Optional[str] = None
    # Who or what the statement is about (e.g. "a 1942 patent, not ZenaDrone").
    subject: Optional[str] = None
    reasoning: str = ""
    confidence: float = 0.0
    recommended_action: Optional[str] = None
    # What the rule check alone would have said, before the context was read.
    rule_decision: FindingStatus = "VIOLATION"
    # AI: the model judged it. ADJUSTED: the model's answer was changed by a safeguard (low confidence, or wording that
    # isn't in the content). UNAVAILABLE: no model could judge it, so the rule's own answer stands.
    source: Literal["AI", "ADJUSTED", "UNAVAILABLE"] = "AI"
    model: Optional[str] = None


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
    # Separate claims beyond the first that the finding's penalty also covers (see EXTRA_CLAIM_PENALTY).
    extra_claims: int = 0
    # When counted_with is set: the points this finding would have cost on its own. If a reviewer dismisses the
    # stricter finding, these count instead, so dismissing one rule doesn't make the shared words free.
    uncounted_penalty: int = 0
    # The rule's exceptions, so the per-moment video flags skip the same excused wording.
    exception_terms: List[str] = Field(default_factory=list)
    # Findings saved before statuses existed are violations.
    status: FindingStatus = "VIOLATION"
    # Set when the AI read the trigger in context (CONTEXT rules).
    evaluation: Optional[ContextEvaluation] = None
    # The sentence the wording sits in and the lines around it, kept so a reviewer's call on this finding can be saved
    # as an example (see finding_precedent_service).
    passage: Optional[str] = None
    context: Optional[str] = None


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


class SourceClaim(BaseModel):
    """One claim the post makes, and whether the source it cites backs it up."""
    claim: str
    # SUPPORTED: the source says it. CONTRADICTED: the source says something different, or less. NOT_FOUND: the source
    # covers the subject but not this. UNCLEAR: could be read either way, so a person decides.
    verdict: Literal["SUPPORTED", "CONTRADICTED", "NOT_FOUND", "UNCLEAR"]
    where: Literal["caption", "image"] = "caption"
    # What the source says about it, in the model's words or a short quote: not always word for word.
    evidence: Optional[str] = None
    # What to change or check, when the verdict isn't SUPPORTED.
    note: Optional[str] = None
    confidence: float = 0.0


class SourceCheck(BaseModel):
    """A source the post cites (or that was attached), and what reading it showed."""
    reference: str
    kind: Literal["URL", "NAMED", "DOCUMENT"]
    where: Literal["caption", "image", "attached"] = "caption"
    cue: Optional[str] = None
    # READ: opened and checked. UNREADABLE: couldn't be opened or read (see message). NOT_PROVIDED: cited by name only.
    status: Literal["READ", "UNREADABLE", "NOT_PROVIDED"]
    title: Optional[str] = None
    url: Optional[str] = None
    published: Optional[str] = None
    # OFFICIAL: government or a regulator's filing. COMPANY: the company's own site. NEWS: a news outlet. OTHER.
    authority: Optional[Literal["OFFICIAL", "COMPANY", "NEWS", "OTHER"]] = None
    message: Optional[str] = None
    claims: List[SourceClaim] = Field(default_factory=list)
    # The model that read it, or None when none was needed or none was available.
    model: Optional[str] = None


class AddedRuleSet(BaseModel):
    """A rule set checked because of what the post says, not because it was chosen."""
    rule_set: str
    because: List[str] = Field(default_factory=list)


class ComplianceCheckResponse(BaseModel):
    is_compliant: bool
    traffic_light: str = "GREEN"  # GREEN, YELLOW, RED
    score: int = 100  # 0 to 100
    # A critical (red) finding blocks publication; the score is then capped so it can't look like a pass.
    blocked: bool = False
    # Part of the check needed the AI and it wasn't working (key, quota, outage): the score is a guess, not a result.
    ai_unavailable: bool = False
    score_cap: Optional[int] = None
    # The score before the cap: 100 minus every finding's penalty.
    raw_score: int = 100
    # The rulebook as of this check (when any rule last changed), to tell whether a saved check is out of date.
    rulebook_version: Optional[str] = None
    # Findings that are violations: what costs points and what "issues to fix" counts.
    violations_count: int = 0
    # Warnings that don't cost points or block: need a person's look, need a source, and triggers cleared by context.
    review_count: int = 0
    evidence_count: int = 0
    cleared_count: int = 0
    # Every finding, whatever its status (see ComplianceViolation.status), so a cleared one can still be shown.
    violations: List[ComplianceViolation] = Field(default_factory=list)
    recommendations: List[str] = Field(default_factory=list)
    rules_checked: int = 0
    # Of rules_checked, the ones the text check can't actually test: a reviewer confirms these when approving.
    manual_review_rules: List[ManualReviewRule] = Field(default_factory=list)
    # The scope the rules were picked by: the audiences (target plus implied; empty when none is set, so every
    # audience's rules ran) and the content parts (empty when not known, so every rule's applies_to matched).
    audiences_checked: List[str] = Field(default_factory=list)
    content_parts: List[str] = Field(default_factory=list)
    # Rule sets the post's wording brought in on top of the chosen ones, each with the wording that did it, e.g.
    # [{"rule_set": "Public Company Disclosure", "because": ["$ZENA", "shareholders"]}].
    added_rule_sets: List[AddedRuleSet] = Field(default_factory=list)
    # Missing on checks saved before signals were read.
    signals: Optional[CheckSignals] = None
    # The sources the post cites, opened and compared with its claims. Empty when it cites none.
    source_checks: List[SourceCheck] = Field(default_factory=list)
