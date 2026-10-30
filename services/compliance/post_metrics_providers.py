"""Where published posts' numbers come from, and what each platform's API can actually give us.

PLATFORM_APIS is the source of truth for the Post Performance page: which numbers each platform returns for a post,
what they're called there, whether the comments themselves can be read (needed to judge comment sentiment), what
other sign of a bad reaction exists, and what access it takes. Researched October 2026 against the platforms' docs;
recheck when connecting a platform for real, since these APIs change often (e.g. Meta replaced impressions with views
in 2025).

The platform talks to one interface. Today that's sample data, because posts aren't published to the platforms from
here yet (publishing is simulated), so there are no real post IDs to look up. The sample data only returns what each
platform's API would, so the page already shows the real shape. When publishing is real and an analytics source is
chosen (each platform's API, or Sprout Social post analytics), implement a provider and set POST_METRICS_PROVIDER.
"""

import hashlib
import math
import os
import random
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional


class MetricsUnavailable(Exception):
    """The metrics source can't be reached or isn't configured."""


# Metric keys: views (impressions where that's what the platform reports), reach, likes, comments, shares, saves,
# clicks, quotes, dislikes, negative_reactions, avg_watch_seconds.
PLATFORM_APIS: Dict[str, Dict[str, Any]] = {
    "INSTAGRAM": {
        "name": "Instagram",
        "api": "Instagram Graph API (media insights)",
        "metrics": ["views", "reach", "likes", "comments", "shares", "saves"],
        "labels": {"saves": "Saves"},
        "comment_text": True,
        "negative_signal": "comments",
        "access": "Instagram business or creator account linked to a Facebook Page; instagram_manage_insights and "
                  "instagram_manage_comments. Impressions were retired in 2025; views replaced them.",
    },
    "FACEBOOK": {
        "name": "Facebook",
        "api": "Facebook Pages API (post insights)",
        "metrics": ["views", "likes", "comments", "shares", "clicks", "negative_reactions"],
        "labels": {"likes": "Reactions", "negative_reactions": "Angry / sad reactions"},
        "comment_text": True,
        "negative_signal": "comments, plus angry and sad reactions",
        "access": "Page admin token with read_insights and pages_read_engagement. Post impressions were retired in "
                  "2025; post_media_view (views) replaced them.",
    },
    "LINKEDIN": {
        "name": "LinkedIn page",
        "api": "LinkedIn Community Management API (organization share statistics)",
        "metrics": ["views", "reach", "likes", "comments", "shares", "clicks"],
        "labels": {"views": "Impressions", "reach": "Unique impressions", "likes": "Reactions", "shares": "Reposts"},
        "comment_text": True,
        "negative_signal": "comments",
        "access": "Community Management API access and a page ADMINISTRATOR's token (rw_organization_admin). Stats "
                  "cover a rolling 12 months.",
    },
    "LINKEDIN_PROFILE": {
        "name": "LinkedIn profile",
        "api": "LinkedIn Member Post Analytics API",
        "metrics": ["views", "reach", "likes", "comments", "shares"],
        "labels": {"views": "Impressions", "reach": "Members reached", "likes": "Reactions", "shares": "Reposts"},
        "comment_text": True,
        "negative_signal": "comments",
        "access": "Personal profiles (e.g. the CEO's) need LinkedIn's approval for the Member Post Analytics API "
                  "(launched July 2025) and the member's consent.",
    },
    "X": {
        "name": "X",
        "api": "X API v2 (post public and private metrics)",
        "metrics": ["views", "likes", "comments", "shares", "quotes", "saves", "clicks"],
        "labels": {"views": "Impressions", "comments": "Replies", "shares": "Reposts", "saves": "Bookmarks",
                   "clicks": "Link clicks"},
        "comment_text": True,
        "negative_signal": "replies",
        "access": "A paid X API plan (the free plan can't read posts). Replies are found by searching the post's "
                  "conversation, which some plans limit to the last 7 days. Clicks need the account's own login.",
    },
    "YOUTUBE": {
        "name": "YouTube",
        "api": "YouTube Data API + YouTube Analytics API",
        "metrics": ["views", "likes", "comments", "shares", "dislikes", "avg_watch_seconds"],
        "labels": {"avg_watch_seconds": "Avg. watch time"},
        "comment_text": True,
        "negative_signal": "comments, plus dislikes (only the channel owner can see them)",
        "access": "Channel owner's OAuth (yt-analytics.readonly) for shares, dislikes and watch time; views, likes, "
                  "comments and comment text come from the Data API.",
    },
    "YOUTUBE_SHORTS": {
        "name": "YouTube Shorts",
        "api": "YouTube Data API + YouTube Analytics API",
        "metrics": ["views", "likes", "comments", "shares", "dislikes", "avg_watch_seconds"],
        "labels": {"avg_watch_seconds": "Avg. watch time"},
        "comment_text": True,
        "negative_signal": "comments, plus dislikes (only the channel owner can see them)",
        "access": "Same as YouTube.",
    },
    "TIKTOK": {
        "name": "TikTok",
        "api": "TikTok Display API (video fields)",
        "metrics": ["views", "likes", "comments", "shares"],
        "labels": {},
        "comment_text": False,
        "negative_signal": None,
        "access": "The Display API returns counts only, so comment sentiment can't be read. Comment text needs "
                  "TikTok's Business API with a TikTok business account.",
    },
    "REDDIT": {
        "name": "Reddit",
        "api": "Reddit API",
        "metrics": ["likes", "comments", "dislikes"],
        "labels": {"likes": "Upvotes", "dislikes": "Downvotes"},
        "comment_text": True,
        "negative_signal": "comments, plus downvotes (upvote ratio)",
        "access": "Reddit API app credentials; Reddit doesn't report views for posts.",
    },
    "WEBSITE": {
        "name": "Website / blog",
        "api": None,
        "metrics": [],
        "labels": {},
        "comment_text": False,
        "negative_signal": None,
        "access": "Not a social platform: page views would come from the website's analytics (e.g. Google "
                  "Analytics), which isn't connected.",
    },
}


def api_for(platform: str, is_ceo_profile: bool = False) -> str:
    """The PLATFORM_APIS key for a post: LinkedIn posts from a personal profile use a different API than pages."""
    if platform == "LINKEDIN" and is_ceo_profile:
        return "LINKEDIN_PROFILE"
    return platform if platform in PLATFORM_APIS else "WEBSITE"


class MetricsProvider:
    name = "base"
    is_mock = False

    async def metrics(self, post_key: str, api: str, published_at: datetime) -> Dict[str, Any]:
        """Lifetime numbers for one post, as its platform's API reports them (only the keys in that platform's
        PLATFORM_APIS metrics), a daily views series where views exist, and, where the comments can be read, recent
        comments and their sentiment: {metrics: {...}, daily_views, comment_samples, comment_sentiment}."""
        raise NotImplementedError

    async def sample_posts(self) -> List[Dict[str, Any]]:
        """Example posts to show while there's no real data (only the sample provider has any)."""
        return []


# ---------------------------------------------------------------------------
# Sample data
# ---------------------------------------------------------------------------

# Rough reach and engagement per platform, so the samples look like each platform.
_SHAPE = {
    "LINKEDIN": (4000, 0.035),
    "LINKEDIN_PROFILE": (9000, 0.04),
    "X": (6000, 0.015),
    "INSTAGRAM": (9000, 0.045),
    "FACEBOOK": (5000, 0.02),
    "TIKTOK": (25000, 0.06),
    "YOUTUBE": (12000, 0.04),
    "YOUTUBE_SHORTS": (18000, 0.05),
    "REDDIT": (0, 0.0),
}

_COMMENTS = {
    "POSITIVE": [
        "This is impressive, great work team.",
        "Love seeing this kind of progress.",
        "Exactly what the industry needs right now.",
        "Great footage, very clear explanation.",
    ],
    "NEUTRAL": [
        "Is this available in Canada yet?",
        "What's the flight time on this?",
        "Following for updates.",
    ],
    "NEGATIVE": [
        "Show real results, not marketing.",
        "Not convinced this is safe around people.",
        "Feels like hype until there are numbers.",
    ],
}

# Example posts while there's no real data: (title, brand, platforms, days ago, tone bias, from the CEO's profile).
_SAMPLE_POSTS = [
    ("Wind farm inspection in Arizona", "ZENADRONE", ["LINKEDIN", "YOUTUBE"], 3, 0.15, False),
    ("Behind the scenes: assembling the ZenaDrone 1000", "ZENADRONE", ["INSTAGRAM", "TIKTOK"], 6, 0.2, False),
    ("Drone as a Service: utilities case study", "DAAS", ["LINKEDIN"], 9, 0.1, False),
    ("Q3 update from our CEO", "CEO_THOUGHT_LEADERSHIP", ["LINKEDIN", "X"], 12, -0.15, True),
    ("Crop spraying demo for Ontario farmers", "ZENADRONE", ["FACEBOOK", "INSTAGRAM"], 15, 0.05, False),
    ("ZooOffice: hybrid work tips", "ZOOOFFICE", ["LINKEDIN"], 19, 0.0, False),
    ("Counter-drone interceptor first look", "ZENATECH", ["X", "YOUTUBE"], 23, -0.3, False),
    ("Aerial cinematography reel", "SOFTWARE", ["INSTAGRAM", "YOUTUBE"], 27, 0.25, False),
]


def _rnd(*parts: Any) -> random.Random:
    return random.Random(int(hashlib.sha256(":".join(map(str, parts)).encode()).hexdigest()[:12], 16))


class SampleMetricsProvider(MetricsProvider):
    """Made-up but plausible numbers: stable for a post, growing with its age the way real posts do (fast at first,
    then levelling off), and limited to what each platform's API would return. Always labelled as sample data."""

    name = "sample"
    is_mock = True

    async def metrics(self, post_key: str, api: str, published_at: datetime) -> Dict[str, Any]:
        spec = PLATFORM_APIS[api]
        has = set(spec["metrics"])
        rnd = _rnd(post_key, api)
        reach, rate = _SHAPE.get(api, (3000, 0.02))
        bias = float(post_key.split("|")[1]) if post_key.startswith("sample:") and "|" in post_key else rnd.uniform(-0.1, 0.2)
        days = max(1, min(30, math.ceil((datetime.now(timezone.utc) - published_at).total_seconds() / 86400)))
        # Most views arrive in the first days, then a small long tail (shares, search, profile visits).
        weights = [(math.exp(-d / 2.5) + 0.015) * rnd.uniform(0.8, 1.2) for d in range(30)]
        scale = reach * rnd.uniform(0.4, 2.2) / sum(weights)
        daily = [int(w * scale) for w in weights[:days]] if "views" in has else []
        views = sum(daily)
        engagement = (views or rnd.randint(150, 900)) * (rate or 1) * rnd.uniform(0.6, 1.5) * (1 + bias)
        likes = int(engagement * 0.72)
        positive = max(5, min(90, int(55 + bias * 100 + rnd.randint(-8, 8))))
        negative = max(2, min(80, int(15 - bias * 60 + rnd.randint(-5, 5))))
        if positive + negative > 100:
            positive = 100 - negative
        neutral = 100 - positive - negative
        all_metrics = {
            "views": views,
            "reach": int(views * rnd.uniform(0.55, 0.85)),
            "likes": likes,
            "comments": int(engagement * 0.09),
            "shares": int(engagement * 0.12),
            "saves": int(engagement * 0.07),
            "clicks": int(views * rnd.uniform(0.005, 0.02)),
            "quotes": int(engagement * 0.02),
            # A worse reception shows as more dislikes / angry reactions, where the platform has them.
            "dislikes": int(likes * max(0.01, negative / 100 * rnd.uniform(0.25, 0.45))),
            "negative_reactions": int(likes * max(0.0, (negative - 8) / 100 * rnd.uniform(0.2, 0.4))),
            "avg_watch_seconds": round(rnd.uniform(6, 48), 1),
        }
        out: Dict[str, Any] = {"metrics": {k: v for k, v in all_metrics.items() if k in has}, "daily_views": daily}
        if spec["comment_text"] and all_metrics["comments"]:
            pool = [(m, t) for m, texts in _COMMENTS.items() for t in texts]
            weight = {"POSITIVE": positive, "NEUTRAL": neutral or 1, "NEGATIVE": negative}
            picked: List[tuple] = []
            while len(picked) < 4:
                choice = rnd.choices(pool, weights=[weight[m] for m, _ in pool])[0]
                if choice not in picked:
                    picked.append(choice)
            out["comment_samples"] = [
                {
                    "author": f"@sample_user_{rnd.randint(100, 999)}",
                    "text": text,
                    "sentiment": mood,
                    "likes": rnd.randint(0, 80),
                    "posted_at": (published_at + timedelta(hours=rnd.randint(1, days * 24))).isoformat(),
                }
                for mood, text in picked
            ]
            out["comment_sentiment"] = {"positive": positive, "neutral": neutral, "negative": negative}
        return out

    async def sample_posts(self) -> List[Dict[str, Any]]:
        now = datetime.now(timezone.utc)
        return [
            {
                # The tone bias rides along in the key so the sample numbers can lean positive or negative.
                "key": f"sample:{i}|{bias}",
                "title": title,
                "brand_code": brand,
                "platforms": platforms,
                "is_ceo_profile": ceo,
                "published_at": (now - timedelta(days=days_ago)).replace(minute=0, second=0, microsecond=0),
            }
            for i, (title, brand, platforms, days_ago, bias, ceo) in enumerate(_SAMPLE_POSTS)
        ]


class LiveMetricsProvider(MetricsProvider):
    """Each platform's own API (or Sprout Social post analytics), once posts are published through connected
    accounts with the access listed in PLATFORM_APIS. Until then it says so instead of returning anything."""

    name = "live"

    async def metrics(self, post_key: str, api: str, published_at: datetime) -> Dict[str, Any]:
        raise MetricsUnavailable("Post analytics aren't connected yet.")


_provider: Optional[MetricsProvider] = None


def get_provider() -> MetricsProvider:
    global _provider
    if _provider is None:
        _provider = LiveMetricsProvider() if os.getenv("POST_METRICS_PROVIDER", "sample").lower() == "live" else SampleMetricsProvider()
    return _provider
