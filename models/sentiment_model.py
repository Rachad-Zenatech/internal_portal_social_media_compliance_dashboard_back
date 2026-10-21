"""Content Sentiment Analysis Models
Editorial read of a submission's tone, brand fit, audience interpretation, controversy and reputational
concerns. Advisory only: it never changes the compliance result or replaces human approval.
"""

from datetime import datetime
from typing import List, Literal, Optional
from uuid import UUID

from pydantic import BaseModel, Field

Level = Literal["NONE", "LOW", "MODERATE", "HIGH"]
Assessment = Literal["NO_CONCERNS", "REVIEW_SUGGESTED", "SIGNIFICANT_CONCERNS", "INSUFFICIENT_INFORMATION"]
SentimentStatus = Literal["WAITING_TRANSCRIPT", "PENDING", "PROCESSING", "COMPLETED", "FAILED"]


class Tone(BaseModel):
    primary: str
    secondary: Optional[str] = None
    intensity: Literal["LOW", "MEDIUM", "HIGH"]
    evidence: List[str] = Field(default_factory=list)


class BrandAlignment(BaseModel):
    rating: Literal["ALIGNED", "PARTIALLY_ALIGNED", "MISALIGNED", "INSUFFICIENT_GUIDANCE"]
    explanation: str


class AudienceInterpretation(BaseModel):
    audience: str
    likely_reading: str
    concern_level: Level


class Controversy(BaseModel):
    """The rulebook's 1-6 controversy dial. Levels 3-4 are the intended range for thought leadership."""
    applies: bool
    level: Optional[int] = Field(None, ge=1, le=6)
    style: Optional[str] = None
    target: Optional[Literal["PROBLEM", "ECONOMICS", "SYSTEM_DESIGN", "PEOPLE_OR_INSTITUTIONS", "MIXED"]] = None
    professional_and_nonpartisan: Optional[bool] = None
    capability_claims_precise: Optional[bool] = None
    defense_buyer_credibility_risk: Optional[Level] = None
    explanation: Optional[str] = None


class SentimentFinding(BaseModel):
    category: Literal["TONE", "BRAND", "AUDIENCE", "CONTROVERSY", "REPUTATION", "CLARITY"]
    severity: Literal["LOW", "MODERATE", "HIGH"]
    title: str
    quote: str
    why_it_matters: str
    basis: Literal["OBSERVED_WORDING", "POSSIBLE_INTERPRETATION"]
    guideline_rule_number: Optional[str] = None
    suggested_revision: Optional[str] = None
    # Filled in by the service after checking the quote against the submitted text.
    source: Optional[Literal["title", "caption", "transcript"]] = None
    start_sec: Optional[float] = None
    end_sec: Optional[float] = None


class SentimentResult(BaseModel):
    summary: str
    overall_assessment: Assessment
    reputational_concern: Level
    tone: Tone
    brand_alignment: BrandAlignment
    audience_interpretations: List[AudienceInterpretation] = Field(default_factory=list)
    controversy: Controversy
    findings: List[SentimentFinding] = Field(default_factory=list)
    information_gaps: List[str] = Field(default_factory=list)
    # Added by the service, not the model.
    guidelines_used: List[str] = Field(default_factory=list)
    unverified_findings_removed: int = 0


class SentimentAnalysis(BaseModel):
    id: UUID
    submission_id: UUID
    version_number: int
    trigger: str
    status: SentimentStatus
    model: Optional[str] = None
    prompt_version: str
    result: Optional[SentimentResult] = None
    error: Optional[str] = None
    # False when the title, caption, transcript, scope or applicable rules changed since this ran.
    is_current: bool = True
    created_at: datetime
    completed_at: Optional[datetime] = None
