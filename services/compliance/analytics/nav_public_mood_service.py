"""Nav Public Mood overview: combines the two public scrapings shown in the navigation page:
1. Financial & public conversation about ZenaTech (NASDAQ: ZENA)
2. Public mood & news reaction based on what is getting posted today.
"""

from datetime import datetime
from typing import Any, Dict, Iterable, List, Optional

from models.compliance.public_mood_model import (
    ListeningTopic,
    NavPublicMoodResponse,
    NavScrapeTrack,
    TodayPostSummary,
    XTrendOpportunity,
)
from services.compliance.analytics import daily_scrape_schedule, post_queries
from services.compliance.analytics.listening_providers import ListeningUnavailable, load_feed
from services.compliance.analytics.zenatech_financial_scrape import FINANCIAL_TRACK, is_financial_topic


def _trend_angle(name: str, snap: Any, rec: str, rec_reason: str, can_jump: bool) -> str:
    if can_jump:
        growth = f" (+{snap.volume_change_pct:.0f}% mentions)" if (snap.volume_change_pct or 0) > 0 else ""
        return (
            f"Jump on trend: {name} is seeing {snap.positive_pct:.0f}% positive sentiment{growth}. "
            f"Great timing to publish educational, demo, or thought-leadership content."
        )
    if rec == "HOLD":
        return (
            f"Avoid / Escalate: {name} has {snap.negative_pct:.0f}% negative sentiment or crisis sensitivity. "
            f"Do not publish promotional posts on this topic right now ({rec_reason})."
        )
    return (
        f"Monitor / Wait: {name} ({snap.positive_pct:.0f}% positive, {snap.neutral_pct:.0f}% neutral, "
        f"{snap.negative_pct:.0f}% negative). {rec_reason or 'Not enough positive evidence yet.'}"
    )


def _search_method(search: Dict[str, Any]) -> Optional[str]:
    if not search.get("keywords") and not search.get("queries"):
        return None
    parts = ["Keyword search of public X posts and news"]
    if search.get("queries"):
        parts.append("live news queries")
    parts.append("matched to the topic by vector similarity")
    return ", ".join(parts)


def build_trend_opportunity(
    t: ListeningTopic,
    *,
    scrape_track: Optional[str] = None,
    post_id: Optional[str] = None,
    post_meta: Optional[Dict[str, Any]] = None,
    url_by_text: Optional[Dict[str, str]] = None,
    search: Optional[Dict[str, Any]] = None,
) -> Optional[XTrendOpportunity]:
    """Convert one ListeningTopic + snapshot into an XTrendOpportunity card."""
    snap = t.snapshot
    if not snap:
        return None
    rec = snap.recommendation or "WAIT"
    rec_reason = snap.recommendation_reason or ""
    can_jump = rec == "POST" and (snap.is_spike or (snap.volume_change_pct or 0) >= 10.0 or snap.positive_pct >= 65.0)
    posts = snap.top_posts
    search = search or {}
    if url_by_text:
        posts = [
            p.model_copy(update={"url": p.url or url_by_text.get(p.text)})
            for p in posts
        ]
    return XTrendOpportunity(
        topic_id=t.id,
        external_id=str(getattr(t, "external_id", "") or t.name.lower().replace(" ", "-")),
        name=t.name,
        description=t.description,
        positive_pct=snap.positive_pct,
        neutral_pct=snap.neutral_pct,
        negative_pct=snap.negative_pct,
        net_sentiment=snap.net_sentiment,
        volume=snap.volume,
        volume_change_pct=snap.volume_change_pct,
        is_spike=snap.is_spike,
        recommendation=rec,
        recommendation_reason=rec_reason,
        can_jump_on_trend=can_jump,
        trend_angle=_trend_angle(t.name, snap, rec, rec_reason, can_jump),
        top_posts=posts,
        scrape_track=scrape_track,
        post_id=post_id,
        post_title=(post_meta or {}).get("title"),
        today_reason=(post_meta or {}).get("today_reason"),
        publish_at=(post_meta or {}).get("publish_at"),
        search_method=_search_method(search),
        search_keywords=[str(k) for k in (search.get("keywords") or [])][:30],
        search_queries=[str(q) for q in (search.get("queries") or [])][:10],
    )


def sort_opportunities(items: List[XTrendOpportunity]) -> List[XTrendOpportunity]:
    items.sort(
        key=lambda o: (
            -int(o.can_jump_on_trend),
            -int(o.is_spike),
            -(o.volume_change_pct or 0.0),
            -o.volume,
        )
    )
    return items


def _source_status(sources: Iterable[Dict[str, Any]], kind_prefix: str, has_trends: bool) -> str:
    relevant = [s for s in sources if str(s.get("kind") or "").startswith(kind_prefix)]
    if not relevant:
        return "READY" if has_trends else "NOT_COLLECTED"
    statuses = {s.get("status") for s in relevant if s.get("status") != "NOT_CONFIGURED"}
    if not statuses:
        return "NOT_COLLECTED"
    if statuses & {"FAILED", "RATE_LIMITED", "AUTH_REQUIRED", "SKIPPED"}:
        return "PARTIAL" if has_trends else next(iter(statuses))
    return "READY" if has_trends else "EMPTY"


def _parse_dt(value: Any) -> Optional[datetime]:
    if isinstance(value, datetime):
        return value
    if isinstance(value, str) and value.strip():
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
    return None


def _matches_today_post(ext: str, today_by_id: Dict[str, Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    if not ext.startswith("post-"):
        return None
    for pid, meta in today_by_id.items():
        if ext == f"post-{pid}" or ext.startswith(f"post-{pid}-context-"):
            return meta
    return None


async def get_nav_public_mood(refresh: bool = False, scrape_state: str = "idle") -> NavPublicMoodResponse:
    """Return both nav scrapings: Financial ZenaTech sentiment + Today's Posts public mood."""
    import asyncio
    from services.compliance.analytics import public_mood_service as pms

    async def _safe_feed() -> Dict[str, Any]:
        try:
            return await load_feed() or {}
        except ListeningUnavailable:
            return {}

    async def _safe_topics():
        try:
            return await pms.list_topics(refresh=refresh)
        except ListeningUnavailable:
            return []

    feed, topics, today_candidates = await asyncio.gather(
        _safe_feed(),
        _safe_topics(),
        post_queries.list_today_post_candidates(),
    )
    today_by_id = {str(p["id"]): p for p in today_candidates}

    feed_by_ext: Dict[str, Dict[str, Any]] = {}
    url_by_ext: Dict[str, Dict[str, str]] = {}
    for ft in feed.get("topics") or []:
        ext = str(ft.get("external_id") or "")
        if not ext:
            continue
        feed_by_ext[ext] = ft
        url_by_ext[ext] = {
            str(c.get("text") or ""): str(c.get("url"))
            for c in ft.get("comments") or []
            if c.get("text") and c.get("url")
        }

    financial_trends: List[XTrendOpportunity] = []
    today_trends: List[XTrendOpportunity] = []

    for t in topics:
        ext = str(t.external_id or "")
        ft = feed_by_ext.get(ext) or {}
        url_map = url_by_ext.get(ext)
        if is_financial_topic({"external_id": ext, **ft}):
            opp = build_trend_opportunity(t, scrape_track=FINANCIAL_TRACK, url_by_text=url_map, search=ft)
            if opp:
                financial_trends.append(opp)
            continue

        matched_today = _matches_today_post(ext, today_by_id)
        # A previous scrape can still contain a private draft. Only display topics for posts that remain eligible.
        if ft.get("scrape_track") == post_queries.TODAY_TRACK and not matched_today:
            continue
        if matched_today or ft.get("scrape_track") == post_queries.TODAY_TRACK:
            meta = matched_today or ft.get("post_meta") or {}
            post_id = str((matched_today or {}).get("id") or meta.get("submission_id") or post_queries.post_id_for_topic(ext) or "") or None
            opp = build_trend_opportunity(
                t,
                scrape_track=post_queries.TODAY_TRACK,
                post_id=post_id,
                post_meta=meta,
                url_by_text=url_map,
                search=ft,
            )
            if opp:
                today_trends.append(opp)

    sort_opportunities(financial_trends)
    sort_opportunities(today_trends)

    collected_at = _parse_dt(feed.get("collected_at"))
    sources = (feed.get("collection") or {}).get("sources") or []

    fin_status = _source_status(sources, "financial", bool(financial_trends))
    today_status = _source_status(sources, "post", bool(today_trends))

    fin_empty = None if financial_trends else "No financial ZenaTech scrape collected yet. Click Scrape financial to pull ZENA news and market sentiment."
    if today_trends:
        today_empty = None
    elif not today_candidates:
        today_empty = "No posts are scheduled or created for today yet. Set a post's Publish Date to today (or create/edit a post today) and click Scrape today's posts."
    elif any(p.get("has_searches") for p in today_candidates):
        today_empty = "Today's posts are queued, but no scraped public mood results are saved for them yet. Click Scrape today's posts to collect live coverage."
    else:
        today_empty = "Today's posts have no named company, product, or campaign keywords yet to search public news."

    return NavPublicMoodResponse(
        financial_zenatech=NavScrapeTrack(
            key="financial_zenatech",
            title="Financial: What they are saying about ZenaTech",
            subtitle="Live Yahoo Finance (NASDAQ: ZENA), financial press, and market sentiment about ZenaTech.",
            status=fin_status,
            collected_at=collected_at,
            empty_reason=fin_empty,
            trends=financial_trends,
        ),
        today_posts=NavScrapeTrack(
            key="today_posts",
            title="Today's posts: Based on what is getting posted today",
            subtitle="Public news & social reaction scraped for posts scheduled or active today.",
            status=today_status,
            collected_at=collected_at,
            empty_reason=today_empty,
            posts=[TodayPostSummary(**p) for p in today_candidates],
            trends=today_trends,
        ),
        scrape_state=scrape_state,
        schedule_enabled=daily_scrape_schedule.DAILY_SCRAPE_ENABLED,
        schedule_label=daily_scrape_schedule.schedule_description(),
        next_scrape_at=daily_scrape_schedule.next_scheduled_run() if daily_scrape_schedule.DAILY_SCRAPE_ENABLED else None,
    )
