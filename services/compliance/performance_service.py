"""Post performance: how published posts are doing, and whether the reaction is still positive.

A post counts as live once it's approved and its publish time has passed. Its numbers come from the metrics provider
(sample data until real publishing and analytics are connected), limited to what each platform's API returns (see
post_metrics_providers.PLATFORM_APIS), and are saved as dated snapshots, refreshed every few hours, so a change in
sentiment between checks can be seen.

Each post on each platform gets a health status from plain rules, using the sign of a bad reaction that platform
actually provides:
- comments (share of negative comments, and how fast it's rising), where the comments can be read;
- dislikes (YouTube, for the channel owner) and downvotes (Reddit);
- angry and sad reactions (Facebook);
- and, everywhere with views, a like rate well below the same platform's other posts.
AT_RISK, WATCH or HEALTHY, always with the reasons in plain words.
"""

import json
import os
import statistics
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple
from uuid import UUID

from models.compliance.performance_model import (
    CommentSentiment,
    PerformancePost,
    PerformanceReport,
    PerformanceSummary,
    PlatformApi,
)
from postgresql_db.database import fetch_all, get_pool
from services.compliance import submission_service
from services.compliance.post_metrics_providers import PLATFORM_APIS, MetricsUnavailable, api_for, get_provider


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, default))
    except ValueError:
        return default


FRESH_HOURS = _env_float("POST_METRICS_FRESH_HOURS", 6)
# Health thresholds. Comments: percent negative, and percentage-point rises between checks.
AT_RISK_NEGATIVE_PCT = _env_float("PERFORMANCE_AT_RISK_NEGATIVE_PCT", 30)
WATCH_NEGATIVE_PCT = _env_float("PERFORMANCE_WATCH_NEGATIVE_PCT", 18)
AT_RISK_NEGATIVE_RISE = _env_float("PERFORMANCE_AT_RISK_NEGATIVE_RISE", 10)
WATCH_NEGATIVE_RISE = _env_float("PERFORMANCE_WATCH_NEGATIVE_RISE", 5)
# Dislikes / downvotes as a share of all likes and dislikes (a few percent is normal on YouTube).
AT_RISK_DISLIKE_PCT = _env_float("PERFORMANCE_AT_RISK_DISLIKE_PCT", 15)
WATCH_DISLIKE_PCT = _env_float("PERFORMANCE_WATCH_DISLIKE_PCT", 8)
# Angry and sad reactions as a share of all reactions (Facebook).
AT_RISK_NEGATIVE_REACTION_PCT = _env_float("PERFORMANCE_AT_RISK_NEGATIVE_REACTION_PCT", 10)
WATCH_NEGATIVE_REACTION_PCT = _env_float("PERFORMANCE_WATCH_NEGATIVE_REACTION_PCT", 5)
# A like rate below this share of the platform's median is worth a look.
LOW_LIKE_RATE_SHARE = _env_float("PERFORMANCE_LOW_LIKE_RATE_SHARE", 0.5)
# Sentiment isn't judged on a handful of comments or reactions.
MIN_COMMENTS = 5
MIN_REACTIONS = 30

ENGAGEMENT_METRICS = ("likes", "comments", "shares", "saves", "quotes")


async def _live_posts(user_id: UUID, team: bool, days: int) -> List[Dict[str, Any]]:
    """Approved posts whose publish time has passed, one row per platform: everyone's for reviewers and managers,
    otherwise the viewer's own."""
    rows = await fetch_all(
        """
        SELECT s.id, s.reference_code, s.title, s.brand_code, s.platforms, s.publish_at,
               b.name AS brand_name, u.full_name AS creator_name,
               s.brand_code = 'CEO_THOUGHT_LEADERSHIP' OR EXISTS (
                   SELECT 1 FROM oauth_connections c
                   WHERE (c.id = ANY(s.account_ids) OR c.id = s.account_id) AND c.provider = 'LINKEDIN_PROFILE'
               ) AS is_ceo_profile
        FROM content_submissions s
        LEFT JOIN brands b ON b.code = s.brand_code
        LEFT JOIN users u ON u.id = s.created_by
        WHERE s.status = 'APPROVED' AND s.deleted_at IS NULL
          AND s.publish_at IS NOT NULL AND s.publish_at <= NOW() AND s.publish_at > NOW() - $1::interval
          AND ($2::boolean OR s.created_by = $3)
        """,
        timedelta(days=days), team, user_id,
    )
    return [
        {
            "key": str(r["id"]), "submission_id": r["id"], "reference_code": r["reference_code"], "title": r["title"],
            "brand_code": r["brand_code"], "brand_name": r["brand_name"], "platform": p, "published_at": r["publish_at"],
            "creator_name": r["creator_name"], "is_sample": False, "is_ceo_profile": bool(r["is_ceo_profile"]),
        }
        for r in rows for p in (r["platforms"] or [])
    ]


async def _snapshots(posts: List[Dict[str, Any]]) -> Dict[Tuple[str, str], List[Dict[str, Any]]]:
    """The latest two snapshots per post and platform (the second one is for the trend)."""
    if not posts:
        return {}
    rows = await fetch_all(
        """
        SELECT * FROM (
            SELECT m.*, row_number() OVER (PARTITION BY post_key, platform ORDER BY fetched_at DESC) AS n
            FROM post_metrics_snapshots m
            WHERE (post_key, platform) IN (SELECT * FROM unnest($1::text[], $2::text[]))
        ) x WHERE n <= 2
        ORDER BY post_key, platform, fetched_at DESC
        """,
        [p["key"] for p in posts], [p["platform"] for p in posts],
    )
    out: Dict[Tuple[str, str], List[Dict[str, Any]]] = defaultdict(list)
    for r in rows:
        out[(r["post_key"], r["platform"])].append(dict(r))
    return out


async def _refresh(posts: List[Dict[str, Any]]) -> Optional[str]:
    """Fetch new numbers for posts whose latest snapshot is older than FRESH_HOURS. Returns a note if the source
    couldn't be reached (the last numbers are shown instead)."""
    provider = get_provider()
    latest = await _snapshots(posts)
    cutoff = datetime.now(timezone.utc) - timedelta(hours=FRESH_HOURS)
    stale = [p for p in posts if not latest.get((p["key"], p["platform"])) or latest[(p["key"], p["platform"])][0]["fetched_at"] < cutoff]
    fetched, note = [], None
    for p in stale:
        try:
            m = await provider.metrics(p["key"], p["api"], p["published_at"])
        except MetricsUnavailable as e:
            note = str(e)
            continue
        nums = m.get("metrics") or {}
        s = m.get("comment_sentiment") or {}
        fetched.append({
            "post_key": p["key"], "submission_id": str(p["submission_id"]) if p.get("submission_id") else None,
            "platform": p["platform"], "api": p["api"], "metrics": nums,
            "views": nums.get("views", 0), "likes": nums.get("likes", 0), "comments": nums.get("comments", 0),
            "shares": nums.get("shares", 0), "saves": nums.get("saves", 0), "daily_views": m.get("daily_views") or [],
            "positive": s.get("positive"), "neutral": s.get("neutral"), "negative": s.get("negative"),
            "samples": m.get("comment_samples") or [],
        })
    if fetched:
        pool = get_pool()
        async with pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO post_metrics_snapshots (post_key, submission_id, platform, api, metrics, views, likes,
                    comments, shares, saves, daily_views, comment_positive_pct, comment_neutral_pct,
                    comment_negative_pct, comment_samples, is_mock)
                SELECT f.post_key, f.submission_id, f.platform, f.api, f.metrics, f.views, f.likes, f.comments,
                       f.shares, f.saves, ARRAY(SELECT jsonb_array_elements_text(f.daily_views)::int), f.positive,
                       f.neutral, f.negative, f.samples, $2
                FROM jsonb_to_recordset($1::jsonb) AS f(
                    post_key text, submission_id uuid, platform text, api text, metrics jsonb, views int, likes int,
                    comments int, shares int, saves int, daily_views jsonb, positive numeric, neutral numeric,
                    negative numeric, samples jsonb
                )
                """,
                json.dumps(fetched), provider.is_mock,
            )
    return note


def _share(part: Optional[int], whole: int) -> Optional[float]:
    return round(part / whole * 100, 1) if part is not None and whole else None


def _health(post: PerformancePost, platform_median_like_rate: Optional[float]) -> None:
    """Sets health, the reasons for it, and what its sentiment is based on."""
    risk, watch, basis = [], [], []
    s = post.sentiment
    if s and post.comments is not None and post.comments >= MIN_COMMENTS:
        basis.append("comments")
        noun = "replies" if post.api == "X" else "comments"
        if s.negative >= AT_RISK_NEGATIVE_PCT:
            risk.append(f"{s.negative:.0f}% of {noun} are negative.")
        elif s.negative >= WATCH_NEGATIVE_PCT:
            watch.append(f"{s.negative:.0f}% of {noun} are negative.")
        if post.negative_change is not None:
            if post.negative_change >= AT_RISK_NEGATIVE_RISE:
                risk.append(f"Negative {noun} are up {post.negative_change:.0f} points since the last check.")
            elif post.negative_change >= WATCH_NEGATIVE_RISE:
                watch.append(f"Negative {noun} are up {post.negative_change:.0f} points since the last check.")
    if post.dislikes is not None and (post.likes or 0) + post.dislikes >= MIN_REACTIONS:
        basis.append("downvotes" if post.api == "REDDIT" else "dislikes")
        word = "downvotes" if post.api == "REDDIT" else "dislikes"
        pct = _share(post.dislikes, (post.likes or 0) + post.dislikes) or 0
        if pct >= AT_RISK_DISLIKE_PCT:
            risk.append(f"{pct:.0f}% of votes are {word}.")
        elif pct >= WATCH_DISLIKE_PCT:
            watch.append(f"{pct:.0f}% of votes are {word}.")
    if post.negative_reactions is not None and (post.likes or 0) >= MIN_REACTIONS:
        basis.append("reactions")
        pct = _share(post.negative_reactions, post.likes or 0) or 0
        if pct >= AT_RISK_NEGATIVE_REACTION_PCT:
            risk.append(f"{pct:.0f}% of reactions are angry or sad.")
        elif pct >= WATCH_NEGATIVE_REACTION_PCT:
            watch.append(f"{pct:.0f}% of reactions are angry or sad.")
    if (platform_median_like_rate and post.views and post.views >= 200 and post.like_rate is not None
            and post.like_rate < platform_median_like_rate * LOW_LIKE_RATE_SHARE):
        watch.append(
            f"Like rate {post.like_rate * 100:.1f}% is well below this platform's usual {platform_median_like_rate * 100:.1f}%."
        )
    post.health = "AT_RISK" if risk else "WATCH" if watch else "HEALTHY"
    post.reasons = risk + watch
    post.sentiment_basis = basis


def _to_post(p: Dict[str, Any], snaps: List[Dict[str, Any]]) -> PerformancePost:
    spec = PLATFORM_APIS[p["api"]]
    post = PerformancePost(
        **{k: p[k] for k in ("submission_id", "reference_code", "title", "brand_code", "brand_name", "platform",
                             "published_at", "creator_name", "is_sample", "api")},
        key=f"{p['key']}:{p['platform']}",
        platform_name=spec["name"],
        available=list(spec["metrics"]),
        labels=dict(spec["labels"]),
        comment_text=spec["comment_text"],
    )
    if not snaps:
        return post
    cur = snaps[0]
    nums = cur["metrics"]
    nums = json.loads(nums) if isinstance(nums, str) else (nums or {})
    for k in ("views", "reach", "likes", "comments", "shares", "saves", "clicks", "quotes", "dislikes",
              "negative_reactions", "avg_watch_seconds"):
        setattr(post, k, nums.get(k) if k in spec["metrics"] else None)
    post.engagements = sum(nums.get(k) or 0 for k in ENGAGEMENT_METRICS if k in spec["metrics"])
    if post.views:
        post.engagement_rate = round(post.engagements / post.views, 4)
        post.like_rate = round((post.likes or 0) / post.views, 4)
    if cur["comment_negative_pct"] is not None:
        post.sentiment = CommentSentiment(
            positive=float(cur["comment_positive_pct"] or 0), neutral=float(cur["comment_neutral_pct"] or 0),
            negative=float(cur["comment_negative_pct"]),
        )
        if len(snaps) > 1 and snaps[1]["comment_negative_pct"] is not None:
            post.negative_change = round(float(cur["comment_negative_pct"]) - float(snaps[1]["comment_negative_pct"]), 1)
    post.daily_views = list(cur["daily_views"] or [])
    samples = cur["comment_samples"]
    post.comment_samples = json.loads(samples) if isinstance(samples, str) else (samples or [])
    post.fetched_at = cur["fetched_at"]
    return post


async def report(user_id: UUID, *, days: int = 30, platform: Optional[str] = None, brand: Optional[str] = None,
                 mine: Optional[bool] = None) -> PerformanceReport:
    """Every live post (one per platform) with its numbers and health, plus totals. Anyone can look at the whole
    team's posts (it's read-only); reviewers and managers start there, creators start with their own."""
    provider = get_provider()
    team = not mine if mine is not None else await submission_service._can_see_team_content(user_id)
    posts = await _live_posts(user_id, team, days)
    if provider.is_mock:
        cutoff = datetime.now(timezone.utc) - timedelta(days=days)
        posts += [
            {**s, "submission_id": None, "reference_code": None, "brand_name": None, "creator_name": None,
             "platform": pl, "is_sample": True}
            for s in await provider.sample_posts() if s["published_at"] > cutoff for pl in s["platforms"]
        ]
    if platform:
        posts = [p for p in posts if p["platform"] == platform]
    if brand:
        posts = [p for p in posts if p["brand_code"] == brand]
    for p in posts:
        p["api"] = api_for(p["platform"], p.get("is_ceo_profile", False))

    # Platforms with no API for post numbers (a website) are listed separately, not shown as zeros.
    unmeasured = [p for p in posts if not PLATFORM_APIS[p["api"]]["metrics"]]
    posts = [p for p in posts if PLATFORM_APIS[p["api"]]["metrics"]]

    out = PerformanceReport(source="Sample data" if provider.is_mock else "Platform APIs", is_mock=provider.is_mock,
                            team_view=team, summary=PerformanceSummary(), unmeasured=len(unmeasured))
    out.platform_apis = [
        PlatformApi(key=k, name=v["name"], api=v["api"], metrics=list(v["metrics"]), labels=dict(v["labels"]),
                    comment_text=v["comment_text"], negative_signal=v["negative_signal"], access=v["access"])
        for k, v in PLATFORM_APIS.items()
    ]
    out.message = await _refresh(posts)
    snaps = await _snapshots(posts)
    built = [_to_post(p, snaps.get((p["key"], p["platform"]), [])) for p in posts]

    by_api: Dict[str, List[float]] = defaultdict(list)
    for b in built:
        if b.views and b.views >= 200 and b.like_rate is not None:
            by_api[b.api].append(b.like_rate)
    for b in built:
        rates = by_api.get(b.api) or []
        _health(b, statistics.median(rates) if len(rates) >= 2 else None)

    order = {"AT_RISK": 0, "WATCH": 1, "HEALTHY": 2}
    built.sort(key=lambda b: (order[b.health], -b.published_at.timestamp()))
    out.posts = built

    s = out.summary
    s.posts = len(built)
    s.views = sum(b.views or 0 for b in built)
    s.engagements = sum(b.engagements for b in built if b.views)
    s.engagement_rate = round(s.engagements / s.views, 4) if s.views else 0
    commented = [b for b in built if b.sentiment and b.comments]
    total_comments = sum(b.comments or 0 for b in commented)
    if total_comments:
        s.positive_pct = round(sum(b.sentiment.positive * (b.comments or 0) for b in commented) / total_comments, 1)
        s.negative_pct = round(sum(b.sentiment.negative * (b.comments or 0) for b in commented) / total_comments, 1)
    s.healthy = sum(1 for b in built if b.health == "HEALTHY")
    s.watch = sum(1 for b in built if b.health == "WATCH")
    s.at_risk = sum(1 for b in built if b.health == "AT_RISK")
    if not built and not out.message:
        out.message = "No posts have gone live in this period yet."
    return out
