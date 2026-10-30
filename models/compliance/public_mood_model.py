"""Public mood: how people online currently feel about the topics a post touches (from social listening), and an
AI reading of what that means for the post. Advisory only."""

from datetime import datetime
from typing import List, Literal, Optional
from uuid import UUID

from pydantic import BaseModel, Field


class ListeningPost(BaseModel):
    network: str
    author: str
    text: str
    sentiment: Literal["POSITIVE", "NEUTRAL", "NEGATIVE"]
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
    fetched_at: datetime


class MatchedTopic(BaseModel):
    topic_id: UUID
    name: str
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


class DismissedTopic(BaseModel):
    topic_id: UUID
    name: str
    by_name: Optional[str] = None
    at: datetime


class PublicMood(BaseModel):
    source: str
    is_mock: bool
    connected: bool
    # Topics were matched by meaning as well as keywords (the vector store is on and reachable).
    matched_by_meaning: bool = False
    # Why there's nothing to show (source not connected, no topics matched).
    message: Optional[str] = None
    topics: List[MatchedTopic] = Field(default_factory=list)
    # Topics someone said this post isn't about; left out until put back.
    dismissed: List[DismissedTopic] = Field(default_factory=list)
    summary: Optional[PublicMoodSummary] = None


class ListeningTopic(BaseModel):
    id: UUID
    name: str
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
