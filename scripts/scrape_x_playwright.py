"""Live Playwright Global Trends & Market News Scraper + VADER Sentiment Analyzer.

Scrapes general, company-agnostic market trends, public X posts from global news/industry accounts
(@AP, @FAANews, @BBCBreaking, @UAVCoach, @JobyAviation, @KratosDefense, @RedCatHoldings), and live
real-time news discussions across broad market & world topics (never scraping ZenaTech's own profiles).

Then classifies each scraped post/headline as POSITIVE, NEGATIVE, or NEUTRAL with confidence scores
the sentiment model (VADER as the fallback), computes POST / WAIT / HOLD recommendations, and stores the
live feed in the vector database's `x_feed_data` collection (a JSON file only when there is no vector database, or
with --out).

Usage:
    python -m scripts.scrape_x_playwright            # stores the feed in x_feed_data
    python -m scripts.scrape_x_playwright --out f.json   # writes a file instead
"""

import argparse
import asyncio
import html
import json
import logging
import os
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional
from urllib.parse import quote_plus

ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

_LOCAL_LIBS = "/home/rachad/.local/pw_libs/usr/lib/x86_64-linux-gnu"
if os.path.isdir(_LOCAL_LIBS) and _LOCAL_LIBS not in os.environ.get("LD_LIBRARY_PATH", ""):
    os.environ["LD_LIBRARY_PATH"] = f"{_LOCAL_LIBS}:{os.environ.get('LD_LIBRARY_PATH', '')}"

try:  # a command-line run needs the same MONGODB_URI the server has (the server's own runs inherit it)
    from dotenv import load_dotenv
    load_dotenv(ROOT_DIR / ".env")
except ImportError:
    pass

from services.compliance.analytics.listening_providers import (
    LOCAL_MODEL_NAME,
    analyze_raw_x_topic,
    classify_comment_sentiment,
    clean_headline,
    evaluate_publishing_decision,
    save_feed,
)

logger = logging.getLogger("scrape_x_playwright")

try:
    NEWS_MAX_AGE_DAYS = int(os.getenv("X_SCRAPE_NEWS_MAX_AGE_DAYS", os.getenv("PUBLIC_MOOD_ITEM_WINDOW_DAYS", "14")))
except ValueError:
    NEWS_MAX_AGE_DAYS = 14

# Broad industry, technology, and global news trends (company-agnostic, zero ZenaTech handles/queries).
TRENDING_MARKET_TOPICS: List[Dict[str, Any]] = [
    {
        "external_id": "trend-attack-drones-warfare",
        "name": "Attack Drones & Military Strikes",
        "description": "Military attack drones, drone raids, airstrikes, counter-UAS interceptors, and geopolitical drone warfare in the news.",
        "keywords": ["attack drone", "attack drones", "drone strike", "drone strikes", "drone raid", "military drone", "counter uas", "counter drone", "interceptor", "airstrike"],
        "x_handles": ["AP", "KratosDefense", "RedCatHoldings"],
        "live_query": "attack drones OR drone strikes OR drone raid casualties warfare",
    },
    {
        "external_id": "trend-ag-drones-farming",
        "name": "Agricultural Drones & Smart Farming",
        "description": "Drones used on farms: crop monitoring, precision spraying, field mapping, agricultural yield and farm automation.",
        "keywords": ["agricultural drone", "agricultural drones", "crop spraying", "precision agriculture", "smart farming", "farm drone", "crop", "crops", "farmers"],
        "x_handles": ["UAVCoach"],
        "live_query": "agricultural drones OR precision agriculture crop spraying farmers",
    },
    {
        "external_id": "trend-autonomous-ai-agents",
        "name": "Autonomous AI & Robotics",
        "description": "AI agents, autonomous robotics, physical AI, automation breakthroughs, and public sentiment around AI adoption.",
        "keywords": ["agentic ai", "ai agents", "autonomous ai", "robotics", "physical ai", "ai automation", "artificial intelligence"],
        "x_handles": ["AP"],
        "live_query": "autonomous AI robotics breakthroughs OR agentic AI automation",
    },
    {
        "external_id": "trend-drone-delivery-logistics",
        "name": "Drone Delivery & Last-Mile Logistics",
        "description": "Delivering packages, food and medical supplies by autonomous drone; last-mile delivery, noise, and city logistics.",
        "keywords": ["drone delivery", "delivery drone", "delivery drones", "last mile", "medical drone delivery", "air taxi", "evtol"],
        "x_handles": ["JobyAviation", "UAVCoach"],
        "live_query": "drone delivery OR medical drone delivery last mile logistics",
    },
    {
        "external_id": "trend-faa-airspace-regulation",
        "name": "FAA Airspace & Drone Regulation",
        "description": "FAA rules, BVLOS (beyond visual line of sight) waivers, airspace safety, flight delays, and aviation regulations.",
        "keywords": ["faa", "part 107", "bvlos", "waiver", "drone regulation", "drone rules", "airspace", "air traffic"],
        "x_handles": ["FAANews"],
        "live_query": "FAA drone regulation BVLOS airspace safety rules",
    },
    {
        "external_id": "trend-commercial-surveying-mapping",
        "name": "Commercial Drone Surveying & Inspection",
        "description": "Using commercial drones for infrastructure inspection, land surveying, 3D mapping, construction, and solar/roof inspections.",
        "keywords": ["drone survey", "drone surveying", "drone inspection", "3d mapping", "lidar drone", "infrastructure inspection", "daas", "drone as a service"],
        "x_handles": ["UAVCoach"],
        "live_query": "commercial drone surveying OR drone infrastructure inspection mapping",
    },
]


def snowflake_time(status_id: str) -> Optional[datetime]:
    """When an X post was made: its id encodes the time (milliseconds since X's 2010 epoch in the top bits)."""
    try:
        ms = (int(status_id) >> 22) + 1288834974657
        return datetime.fromtimestamp(ms / 1000, timezone.utc)
    except (ValueError, OverflowError, OSError):
        return None


def _extract_tweets_from_rendered_profile(body: str, handle: str) -> List[Dict[str, Any]]:
    """Parse X posts from a rendered public X profile: text, link and real post time (from the post id). Engagement
    isn't read: the page's numbers can't be told apart from dates and counts of other posts.

    The page is a list; every post starts at a bullet ("*   [...") and runs to the next one. Inside a post: the
    author header ending in a dated link to the post, then the text, then media and counts."""
    posts: List[Dict[str, Any]] = []
    seen_ids = set()
    status = re.compile(r"https://x\.com/([A-Za-z0-9_]+)/status/(\d+)")
    for block in re.split(r"\n\s*\*\s{2,}(?=\[)", body)[1:]:
        first = status.search(block)
        if not first:
            continue
        author_handle, status_id = first.group(1), first.group(2)
        if status_id in seen_ids:
            continue
        seen_ids.add(status_id)
        posted = snowflake_time(status_id)

        # The text starts after the dated link ("[Nov 10](.../status/ID)", "[3h](...)"), the first one with a label.
        dated = re.search(rf"\[[^\]\n]+\]\(https://x\.com/[A-Za-z0-9_]+/status/{status_id}\)", block)
        chunk = block[dated.end():] if dated else block
        # ...and ends at the first media or empty link (photo, compose box), or the post's own counts.
        chunk = re.split(r"!\[|\[\]\(https://x\.com/", chunk, maxsplit=1)[0]
        chunk = re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", chunk)      # links keep their label
        chunk = re.sub(r"\s+Show more.*$", "", chunk, flags=re.DOTALL)
        chunk = re.sub(r"\s+\d+(?:\s+\d+){0,4}\s*$", "", chunk)      # trailing reply / like counts
        chunk = re.sub(r"\s+", " ", chunk).strip(" *-\n\t")
        if len(chunk) < 25:
            continue

        posts.append({
            "network": "X",
            "author": f"@{author_handle}",
            "status_id": status_id,
            "url": f"https://x.com/{author_handle}/status/{status_id}",
            "text": chunk[:500],
            "engagements": 0,
            "published_at": posted.isoformat() if posted else None,
            "posted_on": posted.date().isoformat() if posted else None,
        })
    return posts


_AUTH_MARKERS = ("log in", "sign in", "sign up", "login required", "create your account")
_RATE_MARKERS = ("429", "rate limit", "too many requests", "unusual traffic")


def _page_status(body: str, found: int) -> str:
    """Why a page gave what it gave: SUCCESS, EMPTY (loaded, nothing there), AUTH_REQUIRED or RATE_LIMITED."""
    if found:
        return "SUCCESS"
    low = (body or "").lower()
    if any(m in low for m in _RATE_MARKERS):
        return "RATE_LIMITED"
    if any(m in low for m in _AUTH_MARKERS):
        return "AUTH_REQUIRED"
    return "EMPTY"


async def _scrape_x_handle_playwright(page, handle: str, limit: int = 20):
    """Scrape a public X profile using Playwright. Returns (posts, status): a failed or blocked profile is told
    apart from one that simply had nothing."""
    url = f"https://r.jina.ai/https://x.com/{handle}"
    try:
        await page.goto(url, wait_until="domcontentloaded", timeout=25000)
        body = await page.inner_text("body")
        parsed = _extract_tweets_from_rendered_profile(body, handle)
        # A logged-out profile can show posts from years ago; only recent ones are evidence of the mood now.
        oldest = datetime.now(timezone.utc) - timedelta(days=NEWS_MAX_AGE_DAYS)
        posts = [p for p in parsed if p["published_at"] and datetime.fromisoformat(p["published_at"]) >= oldest][:limit]
        if len(posts) < len(parsed):
            logger.info("@%s: dropped %d posts older than %d days", handle, len(parsed) - len(posts), NEWS_MAX_AGE_DAYS)
        return posts, _page_status(body, len(posts))
    except Exception as e:
        logger.warning("Playwright failed to scrape @%s: %s", handle, e)
        return [], ("RATE_LIMITED" if any(m in str(e).lower() for m in _RATE_MARKERS) else "FAILED")


def _rss_date_iso(value: Optional[str]) -> Optional[str]:
    """An RSS date ('Tue, 10 Nov 2026 14:03:00 GMT') as an ISO timestamp, or None."""
    if not value:
        return None
    try:
        from email.utils import parsedate_to_datetime
        d = parsedate_to_datetime(value)
        return (d if d.tzinfo else d.replace(tzinfo=timezone.utc)).isoformat()
    except (TypeError, ValueError):
        return None


async def _scrape_live_trend_news_playwright(page, query: str, limit: int = 10, exclude_terms: Optional[List[str]] = None,
                                             drop_own_brand: bool = True):
    """Scrape live breaking news & public headlines via Playwright for a market/world trend query.
    Returns (items, status)."""
    # Google's `when:Nd` keeps to recent articles; without it the RSS returns the most relevant ones of any age
    # (some from years ago), which are not "what people are saying now".
    url = f"https://news.google.com/rss/search?q={quote_plus(f'{query} when:{NEWS_MAX_AGE_DAYS}d')}&hl=en-US&gl=US&ceid=US:en"
    items: List[Dict[str, Any]] = []
    status = "EMPTY"
    oldest = datetime.now(timezone.utc) - timedelta(days=NEWS_MAX_AGE_DAYS)
    try:
        await page.goto(url, wait_until="domcontentloaded", timeout=20000)
        raw_xml = await page.content()
        item_blocks = re.findall(r"<item>(.*?)</item>", raw_xml, flags=re.DOTALL)
        for idx, block in enumerate(item_blocks[:limit]):
            title_m = re.search(r"<title>(.*?)</title>", block, flags=re.DOTALL)
            source_m = re.search(r"<source[^>]*>(.*?)</source>", block, flags=re.DOTALL)
            link_m = re.search(r"<link>(.*?)</link>", block, flags=re.DOTALL)
            date_m = re.search(r"<pubDate>(.*?)</pubDate>", block, flags=re.DOTALL)
            if not title_m:
                continue
            title_txt = html.unescape(re.sub(r"<[^>]+>", "", title_m.group(1))).strip()
            # Filter out any company-specific PR from ZenaTech so trends reflect the general market/news only
            # Market trends stay company-agnostic; searches about the company itself keep its own coverage.
            if drop_own_brand and ("zenatech" in title_txt.lower() or "zenadrone" in title_txt.lower()):
                continue
            if any(t in title_txt.lower() for t in (exclude_terms or [])):
                continue
            published = _rss_date_iso(date_m.group(1).strip() if date_m else None)
            if published and datetime.fromisoformat(published) < oldest:
                continue  # the query asked for recent news, but the guard is cheap
            source_txt = html.unescape(re.sub(r"<[^>]+>", "", source_m.group(1))).strip() if source_m else "News"
            author_slug = re.sub(r"[^A-Za-z0-9_]", "", source_txt.replace(" ", "_"))[:22] or "MarketNews"
            items.append({
                "network": "X",
                "author": f"@{author_slug}",
                "url": link_m.group(1).strip() if link_m else None,
                "text": title_txt,
                # Google News gives no engagement figures, so none are claimed.
                "engagements": 0,
                # The article's own publish time (RSS pubDate), when it has one: the evidence window and the saved
                # assessments depend on it. Items without one are dated from when they were first seen.
                "published_at": _rss_date_iso(date_m.group(1).strip() if date_m else None),
                "posted_on": (_rss_date_iso(date_m.group(1).strip() if date_m else None) or "")[:10] or None,
            })
        status = "SUCCESS" if items else _page_status(raw_xml, 0)
    except Exception as e:
        logger.warning("Playwright live trend scrape failed for %r: %s", query, e)
        status = "RATE_LIMITED" if any(m in str(e).lower() for m in _RATE_MARKERS) else "FAILED"
    return items, status


# ---------------------------------------------------------------------------
# Collection: broad candidates, bounded, with an honest status. Which candidates are about a given draft is decided
# later (embedding + relevance checks), not here.
# ---------------------------------------------------------------------------

def _env_num(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, default))
    except ValueError:
        return default


MAX_ITEMS_PER_TOPIC = int(_env_num("X_SCRAPE_MAX_ITEMS_PER_TOPIC", 30))
MAX_POSTS_PER_HANDLE = int(_env_num("X_SCRAPE_MAX_POSTS_PER_HANDLE", 20))
NEWS_LIMIT = int(_env_num("X_SCRAPE_NEWS_LIMIT", 15))
MAX_SECONDS = _env_num("X_SCRAPE_MAX_SECONDS", 300)
# Words that never make a post a candidate on their own.
_GENERIC = {"that", "this", "with", "from", "into", "over", "your", "have", "will", "about", "more", "than",
            "news", "today", "video", "watch", "live"}


def _csv_env(name: str) -> List[str]:
    return [t.strip().lower() for t in os.getenv(name, "").split(",") if t.strip()]


def _stem(word: str) -> str:
    return word[:-1] if len(word) > 4 and word.endswith("s") else word


def _words(text: str) -> set:
    return {_stem(w) for w in re.findall(r"[a-z0-9]+", (text or "").lower()) if len(w) >= 4 and w not in _GENERIC}


def _exclusions(cfg: Dict[str, Any]) -> List[str]:
    return [*cfg.get("exclude_terms", []), *_csv_env("X_COLLECT_EXCLUDE_TERMS")]


def topic_vocabulary(cfg: Dict[str, Any]) -> set:
    """The words that make an X post a candidate for a topic: any single word from its name, keywords or extra
    collection terms (config `collect_terms`, or X_COLLECT_TERMS for every topic), not only the exact phrases.
    Deliberately broad; the bound is MAX_POSTS_PER_HANDLE / MAX_ITEMS_PER_TOPIC, not strict wording."""
    terms = [cfg["name"], *cfg.get("keywords", []), *cfg.get("collect_terms", []), *_csv_env("X_COLLECT_TERMS")]
    vocab: set = set()
    for t in terms:
        vocab |= _words(t)
    return vocab


def is_candidate(text: str, cfg: Dict[str, Any]) -> bool:
    low = (text or "").lower()
    if any(t.lower() in low for t in _exclusions(cfg)):
        return False
    return bool(_words(text) & topic_vocabulary(cfg))


def overall_status(sources: List[Dict[str, Any]], total_items: int, deadline_hit: bool) -> str:
    """SUCCESS | PARTIAL | EMPTY | AUTH_REQUIRED | RATE_LIMITED | FAILED for the run as a whole. A source that came
    back EMPTY is not a problem (it had nothing) and NOT_CONFIGURED isn't a source at all; a blocked, failed or
    skipped one is."""
    statuses = {s["status"] for s in sources if s["status"] != "NOT_CONFIGURED"}
    if total_items == 0:
        if statuses and statuses <= {"FAILED"}:
            return "FAILED"
        for blocked in ("RATE_LIMITED", "AUTH_REQUIRED"):
            if blocked in statuses and statuses <= {blocked, "EMPTY"}:
                return blocked
        return "EMPTY"
    problem = statuses & {"FAILED", "AUTH_REQUIRED", "RATE_LIMITED", "SKIPPED"}
    return "PARTIAL" if problem or deadline_hit else "SUCCESS"


# The fixed market topics above are only collected when asked for; the default is to search what the drafts say.
FIXED_TOPICS_ON = os.getenv("SCRAPE_FIXED_TOPICS", "off").strip().lower() in ("on", "1", "true", "yes")
WATCH_SCORE = 1000  # a search about the company itself outranks a market topic's claim to the same story


def topic_score(text: str, cfg: Dict[str, Any]) -> int:
    """How strongly a text belongs to a market topic: 2 per topic keyword phrase in it, 1 per shared word."""
    low = (text or "").lower()
    return sum(2 for k in cfg.get("keywords", []) if k.lower() in low) + len(_words(text) & topic_vocabulary(cfg))


def assign_unique(pool: List[Dict[str, Any]], order: List[str]) -> Dict[str, List[Dict[str, Any]]]:
    """One home for each story. `pool` holds candidates {"topic", "score", "item"}: a post that qualifies for several
    topics, or a headline several searches found, goes to the topic that claims it most strongly (ties: the earlier
    topic in `order`), so topics don't share, and double-count, the same story."""
    rank = {t: i for i, t in enumerate(order)}
    best: Dict[str, Dict[str, Any]] = {}
    for cand in pool:
        key = clean_headline(cand["item"]["text"])
        cur = best.get(key)
        if cur is None or (cand["score"], -rank[cand["topic"]]) > (cur["score"], -rank[cur["topic"]]):
            best[key] = cand
    out: Dict[str, List[Dict[str, Any]]] = {t: [] for t in order}
    for cand in best.values():
        out[cand["topic"]].append(cand["item"])
    return out


async def _collect_watch(page, queries, sources, out_of_time, pool) -> List[Dict[str, Any]]:
    """Searches about the company itself (names, hashtags, products, links, events, competitors). Returns the watch
    topics that have a search configured. Every category and every search reports a status."""
    from services.compliance.analytics.listening_sources import CATEGORIES, topic_for

    topics: List[Dict[str, Any]] = []
    for category in CATEGORIES:
        qs = [q for q in queries if q.category == category]
        if not qs:
            sources.append({"kind": category, "name": category, "status": "NOT_CONFIGURED", "items": 0})
            continue
        cfg = topic_for(category, queries)
        found = 0
        for q in qs:
            if out_of_time():
                sources.append({"kind": f"{category}:news", "name": q.text, "status": "SKIPPED", "items": 0})
                continue
            items, status = await _scrape_live_trend_news_playwright(page, q.news, limit=NEWS_LIMIT, drop_own_brand=False)
            sources.append({"kind": f"{category}:news", "name": q.text, "status": status, "items": len(items)})
            for it in items:
                pool.append({"topic": cfg["external_id"], "score": WATCH_SCORE,
                             "item": {**it, "category": category, "collected_by": q.text}})
            found += len(items)
        print(f"[Playwright Live] watch '{category}': {found} candidates")
        topics.append(cfg)
    return topics


def mentions_all(text: str, names: List[str]) -> bool:
    return bool(names) and all(mentions_any(text, [n]) for n in names)


def _plain(text: str) -> str:
    return f" {re.sub(r'[^a-z0-9]+', ' ', (text or '').lower()).strip()} "


def mentions_any(text: str, anchors: List[str]) -> bool:
    low = _plain(text).replace(" u s ", " us ")  # "U.S. Army" and "US Army" are the same name
    return any(_plain(a).replace(" u s ", " us ") in low for a in anchors if a.strip())


def mentions_topic(text: str, terms: List[str]) -> bool:
    """A headline is about a wider topic when it shares at least two of a term's main words ("military drone
    operations" -> military + drone), or the whole term when it is short. Exact phrases are too rare in headlines."""
    words = set(_plain(text).split())
    for term in terms:
        key = [w for w in _plain(term).split() if len(w) >= 4]
        need = 2 if len(key) >= 2 else 1
        if key and sum(1 for w in key if w in words or w.rstrip("s") in {x.rstrip("s") for x in words}) >= need:
            return True
    return False


async def _collect_post_searches(page, searches, sources, out_of_time, pool) -> List[Dict[str, Any]]:
    """News searches built from each draft's own words. Each draft gets a topic of its own; every search reports a
    status. Returns the topics (drafts whose searches found nothing are dropped later, with the reason in sources)."""
    from services.compliance.analytics.post_queries import context_topics_for_draft, topic_for_draft

    topics: List[Dict[str, Any]] = []
    for search in searches:
        cfg = topic_for_draft(search)
        found = 0
        for q in search.get("queries") or []:
            if out_of_time():
                sources.append({"kind": "post:news", "name": q, "status": "SKIPPED", "items": 0})
                continue
            items, status = await _scrape_live_trend_news_playwright(page, q, limit=NEWS_LIMIT, drop_own_brand=False)
            sources.append({"kind": "post:news", "name": q, "status": status, "items": len(items)})
            # Google returns loose matches: keep only headlines that mention one of the draft's own names.
            if cfg.get("require_all"):  # the post names several organisations: the story must name them all
                about = [it for it in items if mentions_all(it["text"], cfg["require_all"])]
            else:
                about = [it for it in items if mentions_any(it["text"], cfg.get("anchors") or [])]
            sources[-1]["dropped_unrelated"] = len(items) - len(about)
            for it in about:
                pool.append({"topic": cfg["external_id"], "score": WATCH_SCORE + topic_score(it["text"], cfg),
                             "item": {**it, "category": "post", "collected_by": q}})
            found += len(about)
        print(f"[Playwright Live] draft {search['title'][:40]!r}: {found} candidates")
        if search.get("queries"):
            topics.append(cfg)
        # The wider topic around the subject (what the public says about it, not about this announcement).
        for ctx in context_topics_for_draft(search):
            kept = 0
            for q in ctx["queries"]:
                if out_of_time():
                    sources.append({"kind": "post:context", "name": q, "status": "SKIPPED", "items": 0})
                    continue
                items, status = await _scrape_live_trend_news_playwright(page, q, limit=NEWS_LIMIT, drop_own_brand=False)
                # Two of the topic's key words are needed, even when the terms are single words ("drone", "army"):
                # a phrase counts on its own, single words only together.
                needs = [t for t in ctx["anchors"] if len(t.split()) >= 2] + [" ".join(ctx["anchors"])]
                about = [it for it in items if mentions_topic(it["text"], needs)]
                sources.append({"kind": "post:context", "name": q, "status": status, "items": len(items),
                                "dropped_unrelated": len(items) - len(about)})
                for it in about:
                    pool.append({"topic": ctx["external_id"], "score": WATCH_SCORE - 1 + topic_score(it["text"], ctx),
                                 "item": {**it, "category": "post-context", "collected_by": q}})
                kept += len(about)
            print(f"[Playwright Live] context {ctx['name'][:50]!r} for {search['title'][:30]!r}: {kept} candidates")
            topics.append(ctx)
    return topics


def read_post_searches() -> List[Dict[str, Any]]:
    from services.compliance.analytics.post_queries import read_file
    return read_file()


async def build_live_x_listening_json(output_path: Optional[Path] = None) -> Dict[str, Any]:
    """Launch Playwright headless Chromium and collect candidates for each market topic and for searches about the
    company itself (bounded by time and item limits), give every story one home, score each with the sentiment model,
    and store the feed with a per-source collection status: in the vector database's x_feed_data collection, or in
    `output_path` (a JSON file) when one is given or there is no vector database. Numbers are only what was
    collected: no estimated volume, daily counts, spike or engagement. The result says where it went (`stored_in`)."""
    import time
    from playwright.async_api import async_playwright
    from services.compliance.analytics.listening_sources import build_queries, load_plan

    started = time.monotonic()
    deadline_hit = False

    def out_of_time() -> bool:
        nonlocal deadline_hit
        if time.monotonic() - started > MAX_SECONDS:
            deadline_hit = True
        return deadline_hit

    handle_cache: Dict[str, List[Dict[str, Any]]] = {}
    sources: List[Dict[str, Any]] = []
    pool: List[Dict[str, Any]] = []
    post_searches = read_post_searches()
    market_topics = TRENDING_MARKET_TOPICS if FIXED_TOPICS_ON else []
    if not post_searches:
        sources.append({"kind": "post", "name": "drafts", "status": "NOT_CONFIGURED", "items": 0})

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        context = await browser.new_context(
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
            )
        )
        page = await context.new_page()

        unique_handles = list(dict.fromkeys(h for t in market_topics for h in t.get("x_handles", [])))
        for handle in unique_handles:
            if out_of_time():
                sources.append({"kind": "x_handle", "name": f"@{handle}", "status": "SKIPPED", "items": 0})
                continue
            posts, status = await _scrape_x_handle_playwright(page, handle, limit=MAX_POSTS_PER_HANDLE)
            handle_cache[handle] = posts
            sources.append({"kind": "x_handle", "name": f"@{handle}", "status": status, "items": len(posts)})
            print(f"[Playwright Live] @{handle}: {len(posts)} X posts ({status})")

        # Market topics: news for each topic's query, and X posts from its accounts.
        for topic_cfg in market_topics:
            tid = topic_cfg["external_id"]
            if out_of_time():
                sources.append({"kind": "news", "name": topic_cfg["name"], "status": "SKIPPED", "items": 0})
            else:
                news, status = await _scrape_live_trend_news_playwright(
                    page, topic_cfg["live_query"], limit=NEWS_LIMIT,
                    exclude_terms=[t.lower() for t in _exclusions(topic_cfg)])
                sources.append({"kind": "news", "name": topic_cfg["name"], "status": status, "items": len(news)})
                print(f"[Playwright Live] {topic_cfg['name']!r}: {len(news)} news items ({status})")
                for item in news:
                    pool.append({"topic": tid, "score": max(1, topic_score(item["text"], topic_cfg)),
                                 "item": {**item, "category": "market", "collected_by": topic_cfg["live_query"]}})
            for h in topic_cfg.get("x_handles", []):
                for post in handle_cache.get(h, []):
                    if is_candidate(post["text"], topic_cfg):
                        pool.append({"topic": tid, "score": topic_score(post["text"], topic_cfg),
                                     "item": {**post, "category": "market", "collected_by": f"@{h}"}})

        # Searches taken from what each draft says (title, caption, script): the main source.
        post_topics = await _collect_post_searches(page, post_searches, sources, out_of_time, pool)

        # No company-wide searches: only what the drafts themselves say is searched.
        await browser.close()

    all_topics = [*post_topics, *market_topics]  # a draft's own searches first: they win ties
    homes = assign_unique(pool, [t["external_id"] for t in all_topics])

    topics_out: List[Dict[str, Any]] = []
    for topic_cfg in all_topics:
        collected = homes[topic_cfg["external_id"]][:MAX_ITEMS_PER_TOPIC]
        if topic_cfg.get("category") and not collected:
            continue  # a company search that found nothing isn't a topic; the sources list says why
        for c in collected:
            cls = classify_comment_sentiment(c["text"])
            c["sentiment"] = cls["sentiment"]
            c["confidence"] = cls["confidence"]
            c["compound"] = cls["compound"]
            c["crisis_flags"] = cls["crisis_flags"]
        # No volume, daily counts or change are passed: analyze_raw_x_topic uses the number collected and leaves
        # the rest unknown (the server works the trend out from everything collected so far).
        analyzed = analyze_raw_x_topic({"comments": collected}, days=7)
        is_spike = (analyzed["volume_change_pct"] or 0) >= 35.0
        rec, rec_reason = evaluate_publishing_decision(
            positive_pct=analyzed["positive"], neutral_pct=analyzed["neutral"], negative_pct=analyzed["negative"],
            volume=analyzed["volume"], volume_change_pct=analyzed["volume_change_pct"], is_spike=is_spike,
            top_posts=analyzed["top_posts"],
        )
        topics_out.append({
            "external_id": topic_cfg["external_id"],
            "name": topic_cfg["name"],
            "description": topic_cfg["description"],
            "keywords": topic_cfg["keywords"],
            "category": topic_cfg.get("category") or "market",
            **analyzed,
            "is_spike": is_spike,
            "recommendation": rec,
            "recommendation_reason": rec_reason,
            "comments": collected,
        })

    total_items = sum(len(t["comments"]) for t in topics_out)
    status = overall_status(sources, total_items, deadline_hit)
    payload = {
        "source": f"Playwright Live Scrape (Global News & Industry X Trends) + sentiment model ({LOCAL_MODEL_NAME})",
        "is_live_scraped": True,
        "collected_at": datetime.now(timezone.utc).isoformat(),
        "collection": {
            "status": status,
            "items": total_items,
            "sources": sources,
            "note": "Stopped at the time limit; later sources were skipped." if deadline_hit else None,
            "limits": {"max_items_per_topic": MAX_ITEMS_PER_TOPIC, "max_posts_per_handle": MAX_POSTS_PER_HANDLE,
                       "news_limit": NEWS_LIMIT, "max_seconds": MAX_SECONDS},
        },
        "topics": topics_out,
    }
    payload["stored_in"] = await save_feed(payload, output_path)
    return payload


def main() -> None:
    parser = argparse.ArgumentParser(description="Live scrape general market/news trends & X posts with Playwright.")
    parser.add_argument("--out", type=Path, default=None,
                        help="Write the feed to this JSON file instead of the vector database.")
    args = parser.parse_args()

    payload = asyncio.run(build_live_x_listening_json(output_path=args.out))
    print(f"\nSaved {len(payload['topics'])} live-scraped market/news trends to {payload['stored_in']} "
          f"(collection: {payload['collection']['status']}, {payload['collection']['items']} items)\n")
    for t in payload["topics"]:
        print(
            f"- {t['name']:38s} | {t['recommendation']:4s} | "
            f"+{t['positive']:4.1f}% / ~{t['neutral']:4.1f}% / -{t['negative']:4.1f}% | "
            f"scraped={len(t['comments'])} items | {t['recommendation_reason']}"
        )


if __name__ == "__main__":
    main()
