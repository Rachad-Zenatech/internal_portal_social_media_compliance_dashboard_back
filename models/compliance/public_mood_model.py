"""Public mood: how people online currently feel about the topics a post touches (from social listening), and an
AI reading of what that means for the post. Advisory only."""

from datetime import datetime
from typing import List, Literal, Optional
from uuid import UUID

from pydantic import BaseModel, Field, computed_field

PublishingRecommendation = Literal["POST", "WAIT", "HOLD"]


class ListeningPost(BaseModel):
    network: str
    author: str
    text: str
    sentiment: Literal["POSITIVE", "NEUTRAL", "NEGATIVE"]
    confidence: Optional[float] = None
    engagements: int = 0
    posted_on: Optional[str] = None


class TopicSnapshot(BaseModel):
    id: UUID
    window_days: int
    positive_pct: float
    neutral_pct: float
    negative_pct: float
    # Positive minus negative, -100 to 100.
    net_sentiment: float
    volume: int
    volume_change_pct: Optional[float] = None
    is_spike: bool = False
    daily_volume: List[int] = Field(default_factory=list)
    top_posts: List[ListeningPost] = Field(default_factory=list)
    recommendation: Optional[PublishingRecommendation] = None
    recommendation_reason: Optional[str] = None
    fetched_at: datetime


class MatchedTopic(BaseModel):
    topic_id: UUID
    name: str
    # own: searched from this post's own subject. context: the wider topic around it (the vibe, not coverage of this
    # announcement; it does not count towards the post's recommendation). other: a company or market topic.
    kind: Literal["own", "context", "other"] = "other"
    # The words in the post that tied it to this topic.
    matched_terms: List[str] = Field(default_factory=list)
    # How close the post is to the topic in meaning (vector search, 0-1), when the vector store is on.
    similarity: Optional[float] = None
    matched_by: Literal["KEYWORD", "MEANING", "BOTH"] = "KEYWORD"
    snapshot: Optional[TopicSnapshot] = None


class TopicReading(BaseModel):
    topic: str
    reading: str
    risk: Literal["LOW", "MEDIUM", "HIGH"]


class PublicMoodResult(BaseModel):
    overall: Literal["FAVOURABLE", "MIXED", "UNFAVOURABLE", "NO_DATA"]
    recommendation: Optional[PublishingRecommendation] = None
    recommendation_reason: Optional[str] = None
    headline: str
    topics: List[TopicReading] = Field(default_factory=list)
    risks: List[str] = Field(default_factory=list)
    opportunities: List[str] = Field(default_factory=list)
    timing_note: Optional[str] = None


MoodVerdict = Literal["USEFUL", "NOT_USEFUL"]


class PublicMoodSummary(BaseModel):
    id: UUID
    version_number: int
    result: PublicMoodResult
    model: Optional[str] = None
    is_mock: bool = False
    created_at: datetime
    # The post or the topics' moods have changed since this was written.
    is_stale: bool = False
    useful_count: int = 0
    not_useful_count: int = 0
    my_verdict: Optional[MoodVerdict] = None


class MoodFeedbackIn(BaseModel):
    verdict: MoodVerdict
    note: Optional[str] = Field(None, max_length=1000)


RelevanceStatus = Literal["RELEVANT", "UNRELATED", "UNCERTAIN"]


class ConversationMatch(BaseModel):
    """A scraped news headline or X post found by vector search, and whether it is about this post's subject."""
    topic: str
    network: str
    author: str
    text: str
    sentiment: Literal["POSITIVE", "NEUTRAL", "NEGATIVE"]
    # Atlas cosine score (0-1) and its lead over the median score of everything retrieved (diagnostic only).
    similarity: float
    margin: float
    posted_on: Optional[str] = None
    item_id: Optional[UUID] = None
    url: Optional[str] = None
    published_at: Optional[datetime] = None
    # Same number as `similarity`: the retrieval score, kept apart from the relevance verdict below.
    similarity_score: Optional[float] = None
    relevance_status: RelevanceStatus = "RELEVANT"
    relevance_reason: Optional[str] = None
    supporting_text: Optional[str] = None
    # Other retrieved items that report the same story (counted once in the mood).
    also_reported_by: int = 0
    # What found it: 'market' (a trend topic) or a watch category (company, hashtags, products...), and the query.
    category: Optional[str] = None
    collected_by: Optional[str] = None


ConversationStatus = Literal["READY", "UNAVAILABLE", "INDEXING_PENDING", "NO_RELEVANT_MATCHES", "STALE"]


class ExcludedMatch(BaseModel):
    """A retrieved item a reviewer marked unrelated: left out of the mood, listed so it can be put back."""
    item_id: UUID
    text: str
    topic: Optional[str] = None
    url: Optional[str] = None
    reason_code: str
    note: Optional[str] = None
    by_name: Optional[str] = None
    at: datetime


class ReviewerCorrection(BaseModel):
    """A reviewer's view that this assessment's recommendation was wrong (the assessment itself is unchanged)."""
    recommendation: PublishingRecommendation
    reason_code: str
    note: Optional[str] = None
    by_name: Optional[str] = None
    at: datetime
    mine: bool = False


class ConversationMood(BaseModel):
    """What the verified-relevant conversations found for this post say: specific to this post, not the topic.

    `status` says what the rest of the object means:
      READY               recommendation, percentages and matches are current.
      NO_RELEVANT_MATCHES conversations were found but none could be verified as about this post: WAIT, never POST.
      INDEXING_PENDING    collected items aren't searchable yet: no recommendation.
      UNAVAILABLE         the vector store or search can't be used right now: no recommendation.
      STALE               an earlier assessment, shown because a current one can't be made (see reason_codes).
    """
    status: ConversationStatus = "READY"
    # Short machine-readable reasons (REVIEW_REQUIRED, SMALL_SAMPLE, RETRIEVAL_CAPPED, COLLECTION_PARTIAL,
    # INDEXING_INCOMPLETE, NO_RELEVANT_EVIDENCE, STORE_NOT_CONFIGURED...) and human-readable warnings.
    reason_codes: List[str] = Field(default_factory=list)
    warnings: List[str] = Field(default_factory=list)
    # Distinct stories the mood was calculated from (not the number displayed).
    count: int = 0
    positive_pct: float = 0.0
    neutral_pct: float = 0.0
    negative_pct: float = 0.0
    recommendation: Optional[PublishingRecommendation] = None
    recommendation_reason: Optional[str] = None
    # The strongest examples, at most PUBLIC_MOOD_DISPLAY_LIMIT. The mood uses every verified story, not just these.
    matches: List[ConversationMatch] = Field(default_factory=list)
    # What happened between search and mood, so a reader can tell a small sample from a capped one.
    retrieved_count: int = 0
    verified_relevant_count: int = 0
    distinct_story_count: int = 0
    displayed_count: int = 0
    uncertain_count: int = 0
    unrelated_count: int = 0
    outdated_count: int = 0
    assessment_scope: str = "matched_conversations"
    # The retrieval limit was reached: a sample of the relevant conversations, not all of them.
    retrieval_capped: bool = False
    assessment_id: Optional[UUID] = None
    evidence_version: Optional[str] = None
    assessed_at: Optional[datetime] = None
    expires_at: Optional[datetime] = None
    # Who decided the recommendation: the RULES, a promoted MODEL (inside guard rails), or RULES_FALLBACK when the model
    # couldn't be used (decision_note says why). A shadow model's answer is saved for comparison but not shown.
    decision_source: str = "RULES"
    decision_note: Optional[str] = None
    # How the evidence was collected and indexed, as of now (not as of the assessment).
    collection_status: Optional[str] = None
    collected_at: Optional[datetime] = None
    indexing_status: Optional[str] = None
    pending_index_count: int = 0
    # Reviewer input on this assessment.
    excluded_by_reviewers: List[ExcludedMatch] = Field(default_factory=list)
    reviewer_corrections: List[ReviewerCorrection] = Field(default_factory=list)

    @computed_field
    @property
    def matched_items(self) -> List[ConversationMatch]:
        """The same list as `matches`, under the name the API contract uses."""
        return self.matches


class UnrelatedIn(BaseModel):
    reason_code: str = Field(..., max_length=40)
    note: Optional[str] = Field(None, max_length=1000)


class SentimentCorrectionIn(BaseModel):
    correct_sentiment: Literal["POSITIVE", "NEUTRAL", "NEGATIVE"]
    note: Optional[str] = Field(None, max_length=1000)


class CorrectionIn(BaseModel):
    recommendation: PublishingRecommendation
    reason_code: str = Field(..., max_length=40)
    note: Optional[str] = Field(None, max_length=1000)


class DismissedTopic(BaseModel):
    topic_id: UUID
    name: str
    by_name: Optional[str] = None
    at: datetime


class ProcessStep(BaseModel):
    """One step of how this post's public mood was made, for the page's process log."""
    title: str
    status: Literal["ok", "warn", "none", "info"] = "info"
    summary: str
    lines: List[str] = Field(default_factory=list)


class PublicMood(BaseModel):
    source: str
    is_mock: bool
    connected: bool
    # Topics were matched by meaning as well as keywords (the vector store is on and reachable).
    matched_by_meaning: bool = False
    # Why there's nothing to show (source not connected, no topics matched).
    message: Optional[str] = None
    topics: List[MatchedTopic] = Field(default_factory=list)
    # Scraped headlines/X posts nearest this post in meaning (vector store on and the feed indexed), with their mood.
    conversation_mood: Optional[ConversationMood] = None
    # Topics someone said this post isn't about; left out until put back.
    dismissed: List[DismissedTopic] = Field(default_factory=list)
    summary: Optional[PublicMoodSummary] = None
    # How this was made, step by step, in plain language.
    process: List[ProcessStep] = Field(default_factory=list)


class ListeningTopic(BaseModel):
    id: UUID
    name: str
    external_id: Optional[str] = None
    description: Optional[str] = None
    # From the listening source.
    keywords: List[str] = Field(default_factory=list)
    # The team's own: extra words that mean this topic, and source keywords that don't count.
    extra_keywords: List[str] = Field(default_factory=list)
    excluded_keywords: List[str] = Field(default_factory=list)
    is_watched: bool = False
    is_mock: bool = False
    synced_at: datetime
    snapshot: Optional[TopicSnapshot] = None
    # Times someone said a post matched to this topic wasn't about it.
    dismissed_count: int = 0
    # The keywords behind those wrong matches, most frequent first.
    dismissed_terms: List[str] = Field(default_factory=list)


class ListeningTopicUpdate(BaseModel):
    extra_keywords: List[str] = Field(default_factory=list, max_length=50)
    excluded_keywords: List[str] = Field(default_factory=list, max_length=50)
    is_watched: bool = False


class MatchTestRequest(BaseModel):
    text: str = Field(..., min_length=1, max_length=6000)


class MatchTestTopic(BaseModel):
    topic_id: UUID
    name: str
    matched: bool
    matched_terms: List[str] = Field(default_factory=list)
    similarity: Optional[float] = None
    # Lead over the text's median topic score; what decides a match by meaning.
    margin: Optional[float] = None
    reason: str


class MatchTestResult(BaseModel):
    matched_by_meaning: bool
    # Matched topics first, then the closest that didn't make it.
    topics: List[MatchTestTopic] = Field(default_factory=list)


class XTrendOpportunity(BaseModel):
    topic_id: Optional[UUID] = None
    external_id: str
    name: str
    description: Optional[str] = None
    positive_pct: float
    neutral_pct: float
    negative_pct: float
    net_sentiment: float
    volume: int
    volume_change_pct: Optional[float] = None
    is_spike: bool = False
    recommendation: PublishingRecommendation
    recommendation_reason: str
    can_jump_on_trend: bool = False
    trend_angle: str
    top_posts: List[ListeningPost] = Field(default_factory=list)


class XCommentInput(BaseModel):
    author: str = "@user"
    text: str = Field(..., min_length=1, max_length=2000)
    engagements: int = Field(0, ge=0)
    posted_on: Optional[str] = None


class XIngestTopicInput(BaseModel):
    external_id: str = Field(..., min_length=1, max_length=200)
    name: str = Field(..., min_length=1, max_length=200)
    description: Optional[str] = None
    keywords: List[str] = Field(default_factory=list)
    volume: Optional[int] = Field(None, ge=0)
    volume_change_pct: Optional[float] = None
    daily_volume: List[int] = Field(default_factory=list)
    comments: List[XCommentInput] = Field(default_factory=list)


class XIngestRequest(BaseModel):
    topics: List[XIngestTopicInput] = Field(..., min_length=1)
