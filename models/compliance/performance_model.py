"""Post performance: how published posts are doing on each platform, and whether the reaction is still positive.
Each platform reports different numbers; a number its API doesn't provide is None, never 0."""

from datetime import datetime
from typing import Dict, List, Literal, Optional
from uuid import UUID

from pydantic import BaseModel, Field

Health = Literal["HEALTHY", "WATCH", "AT_RISK"]


class CommentSample(BaseModel):
    author: str
    text: str
    sentiment: Literal["POSITIVE", "NEUTRAL", "NEGATIVE"]
    likes: int = 0
    posted_at: Optional[datetime] = None


class CommentSentiment(BaseModel):
    positive: float
    neutral: float
    negative: float


class PlatformApi(BaseModel):
    """What one platform's API can give us for a post, and what it takes."""
    key: str
    name: str
    api: Optional[str] = None
    metrics: List[str] = Field(default_factory=list)
    # What the platform calls a metric, where it differs (e.g. views are "Impressions" on LinkedIn and X).
    labels: Dict[str, str] = Field(default_factory=dict)
    # The comments themselves can be read, so their sentiment can be judged.
    comment_text: bool = False
    # Which sign of a bad reaction this platform provides, in words; None when there's none.
    negative_signal: Optional[str] = None
    access: str = ""


class PerformancePost(BaseModel):
    # Submission id (or "sample:N") plus platform: one row per post per platform.
    key: str
    submission_id: Optional[UUID] = None
    reference_code: Optional[str] = None
    title: str
    brand_code: Optional[str] = None
    brand_name: Optional[str] = None
    platform: str
    # Which PlatformApi the numbers come from (a LinkedIn post from a personal profile uses a different API).
    api: str
    platform_name: str
    published_at: datetime
    creator_name: Optional[str] = None
    # An example post shown while there's no real data, not one of ours.
    is_sample: bool = False
    # The metrics this platform provides, and its names for them.
    available: List[str] = Field(default_factory=list)
    labels: Dict[str, str] = Field(default_factory=dict)
    comment_text: bool = False
    views: Optional[int] = None
    reach: Optional[int] = None
    likes: Optional[int] = None
    comments: Optional[int] = None
    shares: Optional[int] = None
    saves: Optional[int] = None
    clicks: Optional[int] = None
    quotes: Optional[int] = None
    dislikes: Optional[int] = None
    negative_reactions: Optional[int] = None
    avg_watch_seconds: Optional[float] = None
    # Likes + comments + shares + saves + quotes, where the platform has them.
    engagements: int = 0
    # Engagements / views, and likes / views; None where the platform reports no views.
    engagement_rate: Optional[float] = None
    like_rate: Optional[float] = None
    sentiment: Optional[CommentSentiment] = None
    # Change in the share of negative comments since the previous check, in percentage points.
    negative_change: Optional[float] = None
    health: Health = "HEALTHY"
    # Why it's flagged, in plain words; empty when healthy.
    reasons: List[str] = Field(default_factory=list)
    # What the health is based on: "comments", "dislikes", "downvotes", "reactions". Empty: no sentiment signal
    # (e.g. TikTok's standard API has no comment text), so only the like rate is checked.
    sentiment_basis: List[str] = Field(default_factory=list)
    daily_views: List[int] = Field(default_factory=list)
    comment_samples: List[CommentSample] = Field(default_factory=list)
    fetched_at: Optional[datetime] = None


class PerformanceSummary(BaseModel):
    posts: int = 0
    views: int = 0
    engagements: int = 0
    # Across posts with views: total engagements / total views.
    engagement_rate: float = 0
    # Comment-weighted share of positive and negative comments.
    positive_pct: Optional[float] = None
    negative_pct: Optional[float] = None
    healthy: int = 0
    watch: int = 0
    at_risk: int = 0


class PerformanceReport(BaseModel):
    source: str
    is_mock: bool
    # Why there's nothing to show, or a note about the data.
    message: Optional[str] = None
    # Whether this shows every post or only the viewer's own (reviewers and managers start with every post,
    # creators with their own; anyone can switch).
    team_view: bool = False
    summary: PerformanceSummary
    posts: List[PerformancePost] = Field(default_factory=list)
    # Posts on platforms with no API for post numbers (e.g. the website), left out of the numbers.
    unmeasured: int = 0
    platform_apis: List[PlatformApi] = Field(default_factory=list)
