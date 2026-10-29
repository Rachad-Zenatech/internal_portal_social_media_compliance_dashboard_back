"""Content Sentiment Analysis Models
Editorial read of a submission's tone, brand fit, audience interpretation, controversy and reputational
concerns. Advisory only: it never changes the compliance result or replaces human approval.
"""

from datetime import datetime
from typing import Dict, List, Literal, Optional
from uuid import UUID

from pydantic import BaseModel, Field

Level = Literal["NONE", "LOW", "MODERATE", "HIGH"]
Assessment = Literal["NO_CONCERNS", "REVIEW_SUGGESTED", "SIGNIFICANT_CONCERNS", "INSUFFICIENT_INFORMATION"]
# SCHEDULED: Gemini's daily limit was used up and the reviewer asked to run it once the limit resets.
SentimentStatus = Literal["WAITING_TRANSCRIPT", "SCHEDULED", "PENDING", "PROCESSING", "COMPLETED", "FAILED"]
FindingVerdict = Literal["USEFUL", "NOT_AN_ISSUE"]


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
    # Stable within an analysis (category + wording + time), so reviewer feedback can point at it.
    id: Optional[str] = None
    category: Literal["TONE", "BRAND", "AUDIENCE", "CONTROVERSY", "REPUTATION", "CLARITY"]
    severity: Literal["LOW", "MODERATE", "HIGH"]
    title: str
    quote: str
    why_it_matters: str
    # OBSERVED_VISUAL / OBSERVED_AUDIO: about what an attached image or the video shows, or how the video sounds;
    # quote then describes what is shown or heard rather than quoting words.
    basis: Literal["OBSERVED_WORDING", "POSSIBLE_INTERPRETATION", "OBSERVED_VISUAL", "OBSERVED_AUDIO"]
    guideline_rule_number: Optional[str] = None
    suggested_revision: Optional[str] = None
    # Filled in by the service after checking the quote against the submitted text ("video" / "image" for findings
    # about what is shown or heard).
    source: Optional[Literal["title", "caption", "transcript", "video", "image"]] = None
    # OBSERVED_VISUAL findings about an image: which one (1 = the first image attached, in the post's order).
    image_index: Optional[int] = None
    # A past review this finding follows (e.g. "SUB-1012 v2"), when the same issue was raised or cleared before.
    precedent: Optional[str] = None
    start_sec: Optional[float] = None
    end_sec: Optional[float] = None


class AudioAssessment(BaseModel):
    """How the video sounds, when one was attached."""
    voiceover: Optional[str] = None
    music: Optional[str] = None
    sound_effects: Optional[str] = None
    # Whether the sound supports the message (None when there's nothing to judge, e.g. a silent video).
    fits_message: Optional[bool] = None
    explanation: Optional[str] = None


class Precedent(BaseModel):
    """A past review of similar content that the analysis was given as precedent."""
    label: str  # e.g. "SUB-1012 v2"
    submission_id: Optional[UUID] = None
    reference_code: Optional[str] = None
    version: Optional[int] = None
    title: Optional[str] = None
    decision: str  # APPROVE | REQUEST_CHANGES
    notes: Optional[str] = None
    decided_at: Optional[datetime] = None
    similarity: float
    # An earlier version of this same post.
    same_post: bool = False


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
    audio: Optional[AudioAssessment] = None
    # Added by the service, not the model.
    precedents: List[Precedent] = Field(default_factory=list)
    guidelines_used: List[str] = Field(default_factory=list)
    unverified_findings_removed: int = 0
    # The model that actually did the analysis, and why, when it wasn't the first choice (e.g. its daily limit was
    # used up), so reviewers know a different model produced it.
    model_used: Optional[str] = None
    model_note: Optional[str] = None


class FindingFeedback(BaseModel):
    """Reviewers' votes on one finding, and the viewer's own."""
    useful: int = 0
    not_an_issue: int = 0
    mine: Optional[FindingVerdict] = None
    my_note: Optional[str] = None


class FindingFeedbackIn(BaseModel):
    verdict: FindingVerdict
    note: Optional[str] = Field(None, max_length=1000)


class AnalysedImage(BaseModel):
    index: int
    name: str


class SentimentComparison(BaseModel):
    """How an analysis differs from the one before it (an earlier version, or an earlier run of this draft)."""
    previous_analysis_id: UUID
    previous_version: int
    # True when the previous run was of the same version (e.g. the draft was edited and re-analysed).
    same_version: bool
    previous_completed_at: Optional[datetime] = None
    previous_assessment: Assessment
    previous_concern: Level
    # Findings from the previous run that no longer come up, so they look fixed.
    resolved: List[SentimentFinding] = Field(default_factory=list)
    # Ids of this run's findings that are new, and of those that were already flagged last time.
    new_ids: List[str] = Field(default_factory=list)
    still_ids: List[str] = Field(default_factory=list)


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
    # False when the title, caption, transcript, scope, applicable rules or messaging context changed since this ran.
    is_current: bool = True
    # The Messaging Context entries it was given (e.g. "LinkedIn", "Investors"), so reviewers can see what it knew.
    context_used: List[str] = Field(default_factory=list)
    # CEO or investor content, analysed with the stronger model.
    high_stakes: bool = False
    # A video was analysed, so findings with a time can show that frame.
    has_video: bool = False
    # The images it looked at, in order (a finding's image_index points into this).
    images: List[AnalysedImage] = Field(default_factory=list)
    # Compared with the previous completed analysis, when there is one.
    comparison: Optional[SentimentComparison] = None
    # Reviewer feedback per finding id (with the viewer's own vote).
    feedback: Dict[str, FindingFeedback] = Field(default_factory=dict)
    # Failed because Gemini's daily limit was used up: when it resets. SCHEDULED: when it will run.
    retry_at: Optional[datetime] = None
    created_at: datetime
    completed_at: Optional[datetime] = None


class FeedbackSummaryRow(BaseModel):
    """How reviewers rated one kind of finding: mostly "not an issue" means that kind is mostly noise."""
    category: str
    basis: str
    rated: int
    useful: int
    not_an_issue: int


class EvalCaseIn(BaseModel):
    """Save an analysed submission as a test case, with the verdict a reviewer agrees is right."""
    analysis_id: UUID
    expected_assessment: Assessment
    expected_concern: Optional[Level] = None
    notes: Optional[str] = Field(None, max_length=2000)


class EvalCase(BaseModel):
    id: UUID
    submission_id: Optional[UUID] = None
    reference_code: Optional[str] = None
    title: Optional[str] = None
    expected_assessment: Assessment
    expected_concern: Optional[Level] = None
    # Wording or moments the analysis should (and shouldn't) flag, taken from reviewers' feedback.
    must_flag: List[str] = Field(default_factory=list)
    must_not_flag: List[str] = Field(default_factory=list)
    notes: Optional[str] = None
    created_by_name: Optional[str] = None
    created_at: datetime
