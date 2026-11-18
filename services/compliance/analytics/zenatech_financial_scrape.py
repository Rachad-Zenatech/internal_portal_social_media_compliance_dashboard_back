"""Financial & company public-sentiment scraping definitions for ZenaTech (NASDAQ: ZENA).

Runs inside scripts/scrape_x_playwright.py when collecting public mood for the nav so teams always see what
investors, analysts, financial press, and media are saying about ZenaTech alongside today's scheduled posts.
"""

from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
import html
import re
from typing import Any, Dict, List, Optional

FINANCIAL_TRACK = "financial_zenatech"
FINANCIAL_TOPIC_PREFIX = "financial-zenatech"

YAHOO_FINANCE_ZENA_RSS = "https://feeds.finance.yahoo.com/rss/2.0/headline?s=ZENA&region=US&lang=en-US"

# Headlines from broad financial searches must name ZenaTech or a company entity/ticker (drops template noise).
ZENATECH_ANCHORS = (
    "zenatech",
    "zenadrone",
    "zena studios",
    "zenastudios",
    "zoooffice",
    "$zena",
    "nasdaq: zena",
    "nasdaq zena",
    "zena stock",
    "(zena)",
)

FINANCIAL_TOPICS: List[Dict[str, Any]] = [
    {
        "external_id": "financial-zenatech-stock",
        "name": "ZenaTech (NASDAQ: ZENA) Financial & Stock Sentiment",
        "description": (
            "Financial news, stock reaction, earnings, valuation, and analyst or investor commentary about "
            "ZenaTech (NASDAQ: ZENA)."
        ),
        "keywords": [
            "ZenaTech",
            "ZENA",
            "$ZENA",
            "NASDAQ: ZENA",
            "stock",
            "shares",
            "earnings",
            "investor",
            "analyst",
            "valuation",
            "forecast",
            "quarter",
            "revenue",
            "Nasdaq",
            "Wall Street",
        ],
        "category": "financial",
        "scrape_track": FINANCIAL_TRACK,
        "rss_feeds": [
            {"name": "Yahoo Finance (ZENA)", "url": YAHOO_FINANCE_ZENA_RSS, "network": "Yahoo Finance", "author": "@YahooFinance"},
        ],
        "live_queries": [
            '("ZenaTech" OR "NASDAQ: ZENA" OR "$ZENA") (stock OR shares OR earnings OR investor OR analyst OR forecast OR valuation OR market)',
            '"ZenaTech" (revenue OR quarter OR Q1 OR Q2 OR Q3 OR Q4 OR Nasdaq OR "Wall Street")',
        ],
    },
    {
        "external_id": "financial-zenatech-buzz",
        "name": "What Market & Public Media Say About ZenaTech",
        "description": (
            "Market, customer, partnership, acquisition, and public news discussion mentioning ZenaTech or ZenaDrone."
        ),
        "keywords": [
            "ZenaTech",
            "ZenaDrone",
            "ZooOffice",
            "Zena Studios",
            "Drone as a Service",
            "contract",
            "acquisition",
            "partnership",
            "announcement",
        ],
        "category": "financial",
        "scrape_track": FINANCIAL_TRACK,
        "rss_feeds": [],
        "live_queries": [
            '("ZenaTech" OR "ZenaDrone")',
        ],
    },
]


def _plain(text: str) -> str:
    return f" {re.sub(r'[^a-z0-9$:().]+', ' ', (text or '').lower()).strip()} "


def is_zenatech_relevant(text: str) -> bool:
    """True when the headline clearly refers to ZenaTech, ZenaDrone, or the ZENA ticker."""
    low = (text or "").lower()
    if "symbol__" in low:
        return False
    padded = _plain(text)
    return any(anchor in low if anchor.startswith(("$", "(")) else anchor in padded for anchor in ZENATECH_ANCHORS)


def rss_date_iso(value: Optional[str]) -> Optional[str]:
    """Parse an RSS pubDate string into an aware UTC ISO timestamp, or None."""
    if not value:
        return None
    try:
        dt = parsedate_to_datetime(value)
        return (dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)).isoformat()
    except (TypeError, ValueError):
        return None


def parse_rss_items(
    raw_xml: str,
    *,
    limit: int,
    max_age_days: int,
    default_network: str = "Financial News",
    default_author: str = "@MarketNews",
    require_anchor: bool = False,
    drop_own_brand: bool = False,
    exclude_terms: Optional[List[str]] = None,
) -> List[Dict[str, Any]]:
    """Extract bounded, recent RSS items with optional ZenaTech or exclusion filtering."""
    oldest = datetime.now(timezone.utc) - timedelta(days=max_age_days)
    items: List[Dict[str, Any]] = []
    for block in re.findall(r"<item>(.*?)</item>", raw_xml or "", flags=re.DOTALL):
        title_m = re.search(r"<title>(.*?)</title>", block, flags=re.DOTALL)
        if not title_m:
            continue
        title_txt = html.unescape(re.sub(r"<[^>]+>", "", title_m.group(1))).strip()
        low = title_txt.lower()
        if not title_txt or "symbol__" in low:
            continue
        if drop_own_brand and ("zenatech" in low or "zenadrone" in low):
            continue
        if any(t in low for t in (exclude_terms or [])):
            continue
        if require_anchor and not is_zenatech_relevant(title_txt):
            continue
        date_m = re.search(r"<pubDate>(.*?)</pubDate>", block, flags=re.DOTALL)
        published = rss_date_iso(date_m.group(1).strip() if date_m else None)
        if published and datetime.fromisoformat(published) < oldest:
            continue
        link_m = re.search(r"<link>(.*?)</link>", block, flags=re.DOTALL)
        source_m = re.search(r"<source[^>]*>(.*?)</source>", block, flags=re.DOTALL)
        source_txt = html.unescape(re.sub(r"<[^>]+>", "", source_m.group(1))).strip() if source_m else ""
        author_slug = re.sub(r"[^A-Za-z0-9_]", "", source_txt.replace(" ", "_"))[:22] if source_txt else ""
        url = link_m.group(1).strip() if link_m else None
        items.append({
            "network": default_network,
            "author": f"@{author_slug}" if author_slug else default_author,
            "url": url,
            "text": title_txt,
            "source_links": [url] if url else [],
            "image_urls": [],
            "image_text": "",
            "engagements": 0,
            "published_at": published,
            "posted_on": (published or "")[:10] or None,
        })
        if len(items) >= limit:
            break
    return items


def is_financial_topic(topic: Dict[str, Any]) -> bool:
    ext = str(topic.get("external_id") or "")
    return topic.get("scrape_track") == FINANCIAL_TRACK or topic.get("category") == "financial" or ext.startswith(FINANCIAL_TOPIC_PREFIX)


def merge_scoped_feed(
    scope: str,
    existing: Optional[Dict[str, Any]],
    new_topics: List[Dict[str, Any]],
    new_sources: List[Dict[str, Any]],
) -> tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    """Preserve the other nav track's topics & sources when only one scrape track is re-run."""
    if scope == "all" or not isinstance(existing, dict):
        return new_topics, new_sources
    old_topics = existing.get("topics") or []
    old_sources = (existing.get("collection") or {}).get("sources") or []
    if scope == "financial":
        kept_topics = [t for t in old_topics if not is_financial_topic(t)]
        kept_sources = [s for s in old_sources if not str(s.get("kind") or "").startswith("financial")]
        return [*new_topics, *kept_topics], [*new_sources, *kept_sources]
    if scope == "today":
        kept_topics = [t for t in old_topics if is_financial_topic(t)]
        kept_sources = [s for s in old_sources if str(s.get("kind") or "").startswith("financial")]
        return [*kept_topics, *new_topics], [*kept_sources, *new_sources]
    return new_topics, new_sources


async def collect_financial_topics(
    fetch_rss,
    fetch_news,
    sources: List[Dict[str, Any]],
    out_of_time,
    pool: List[Dict[str, Any]],
    *,
    limit: int = 15,
    watch_score: int = 1000,
    score_fn=None,
) -> List[Dict[str, Any]]:
    """Collect financial, stock, and media discussion about ZenaTech & ZENA via RSS + news search callbacks."""
    topics: List[Dict[str, Any]] = []
    for cfg in FINANCIAL_TOPICS:
        tid = cfg["external_id"]
        topic_meta = {
            "external_id": tid,
            "name": cfg["name"],
            "description": cfg["description"],
            "keywords": cfg["keywords"],
            "category": "financial",
            "scrape_track": FINANCIAL_TRACK,
        }
        for rss in cfg.get("rss_feeds") or []:
            if out_of_time():
                sources.append({"kind": "financial:rss", "name": rss["name"], "status": "SKIPPED", "items": 0})
                continue
            items, status = await fetch_rss(rss["url"], rss.get("network", "Yahoo Finance"), rss.get("author", "@YahooFinance"))
            sources.append({"kind": "financial:rss", "name": rss["name"], "status": status, "items": len(items)})
            for it in items[:limit]:
                sc = watch_score + (score_fn(it["text"], topic_meta) if score_fn else 1)
                pool.append({"topic": tid, "score": sc, "item": {**it, "category": "financial", "collected_by": rss["name"]}})
        for q in cfg.get("live_queries") or []:
            if out_of_time():
                sources.append({"kind": "financial:news", "name": q, "status": "SKIPPED", "items": 0})
                continue
            items, status = await fetch_news(q)
            about = [it for it in items if is_zenatech_relevant(it.get("text", ""))]
            sources.append({
                "kind": "financial:news",
                "name": q,
                "status": status,
                "items": len(items),
                "dropped_unrelated": len(items) - len(about),
            })
            for it in about:
                sc = watch_score + (score_fn(it["text"], topic_meta) if score_fn else 1)
                pool.append({"topic": tid, "score": sc, "item": {**it, "category": "financial", "collected_by": q}})
        topics.append(topic_meta)
    return topics
