"""Live Playwright Financial ZenaTech & Today's Posts News Scraper + Sentiment Analyzer.

Scrapes financial/media coverage about ZenaTech (NASDAQ: ZENA) and news around today's scheduled/active posts,
classifies each item as POSITIVE, NEGATIVE, or NEUTRAL via the sentiment model (VADER fallback), computes
POST / WAIT / HOLD recommendations, and stores the feed in the vector database's `x_feed_data` collection
(or a JSON file with --out / when no vector database is configured).

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

from scripts.scrape_x_links import enrich_items
from scripts.scrape_x_media import (
    _extract_tweets_from_rendered_profile,
    media_fields,
    snowflake_time,
)
from services.compliance.analytics.listening_providers import (
    analyze_raw_x_topic,
    classify_topic_sentiment,
    clean_headline,
    evaluate_publishing_decision,
    load_feed,
    save_feed,
    sentiment_models_label,
)
from services.compliance.analytics.zenatech_financial_scrape import (
    collect_financial_topics,
    merge_scoped_feed,
    parse_rss_items,
    rss_date_iso as _rss_date_iso,
)

logger = logging.getLogger("scrape_x_playwright")

try:
    NEWS_MAX_AGE_DAYS = int(os.getenv("X_SCRAPE_NEWS_MAX_AGE_DAYS", os.getenv("PUBLIC_MOOD_ITEM_WINDOW_DAYS", "14")))
except ValueError:
    NEWS_MAX_AGE_DAYS = 14


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


async def _scrape_live_trend_news_playwright(page, query: str, limit: int = 10, exclude_terms: Optional[List[str]] = None,
                                             drop_own_brand: bool = True):
    """Scrape live breaking news & public headlines via Playwright for a market/world trend query."""
    url = f"https://news.google.com/rss/search?q={quote_plus(f'{query} when:{NEWS_MAX_AGE_DAYS}d')}&hl=en-US&gl=US&ceid=US:en"
    try:
        await page.goto(url, wait_until="domcontentloaded", timeout=20000)
        raw_xml = await page.content()
        items = parse_rss_items(
            raw_xml, limit=limit, max_age_days=NEWS_MAX_AGE_DAYS,
            default_network="X", default_author="@News",
            drop_own_brand=drop_own_brand, exclude_terms=exclude_terms,
        )
        return items, ("SUCCESS" if items else _page_status(raw_xml, 0))
    except Exception as e:
        logger.warning("Playwright live trend scrape failed for %r: %s", query, e)
        return [], ("RATE_LIMITED" if any(m in str(e).lower() for m in _RATE_MARKERS) else "FAILED")


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


WATCH_SCORE = 1000  # a search about the company or its posts outranks general topic scores


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


async def _scrape_rss_playwright(page, url: str, network: str, author: str):
    """Fetch an RSS feed through Playwright and return parsed recent items and status."""
    try:
        await page.goto(url, wait_until="domcontentloaded", timeout=20000)
        raw_xml = await page.content()
        items = parse_rss_items(raw_xml, limit=NEWS_LIMIT, max_age_days=NEWS_MAX_AGE_DAYS,
                                default_network=network, default_author=author)
        return items, ("SUCCESS" if items else _page_status(raw_xml, 0))
    except Exception as e:
        logger.warning("Playwright RSS scrape failed for %r: %s", url, e)
        return [], ("RATE_LIMITED" if any(m in str(e).lower() for m in _RATE_MARKERS) else "FAILED")


async def _collect_financial(page, sources, out_of_time, pool) -> List[Dict[str, Any]]:
    """Scrape financial, stock, and media coverage about ZenaTech & NASDAQ: ZENA."""
    return await collect_financial_topics(
        lambda url, net, auth: _scrape_rss_playwright(page, url, net, auth),
        lambda q: _scrape_live_trend_news_playwright(page, q, limit=NEWS_LIMIT, drop_own_brand=False),
        sources, out_of_time, pool, limit=NEWS_LIMIT, watch_score=WATCH_SCORE, score_fn=topic_score,
    )


def _topic_entry(topic_cfg: Dict[str, Any], analyzed: Dict[str, Any], is_spike: bool,
                 rec: str, rec_reason: str, collected: List[Dict[str, Any]]) -> Dict[str, Any]:
    entry: Dict[str, Any] = {
        "external_id": topic_cfg["external_id"], "name": topic_cfg["name"],
        "description": topic_cfg["description"], "keywords": topic_cfg["keywords"],
        "category": topic_cfg.get("category") or "market", **analyzed,
        "is_spike": is_spike, "recommendation": rec, "recommendation_reason": rec_reason,
        "comments": collected,
    }
    queries = topic_cfg.get("live_queries") or topic_cfg.get("queries")
    if queries:
        entry["queries"] = list(queries)
    for key in ("scrape_track", "post_meta"):
        if topic_cfg.get(key):
            entry[key] = topic_cfg[key]
    return entry


async def build_live_x_listening_json(output_path: Optional[Path] = None) -> Dict[str, Any]:
    """Launch Playwright headless Chromium and collect candidates for ZenaTech financial coverage and today's/draft
    posts (controlled via SCRAPE_SCOPE=all|financial|today), score sentiment, and store the feed."""
    import time
    from playwright.async_api import async_playwright
    from services.compliance.analytics.listening_sources import CATEGORIES  # noqa: F401
    from services.compliance.analytics.post_queries import assign_unique_per_post
    from services.shared.core.browser_stealth import new_stealth_context

    scope = os.getenv("SCRAPE_SCOPE", "all").strip().lower()
    scope = scope if scope in ("all", "financial", "today") else "all"
    started, deadline_hit = time.monotonic(), False

    def out_of_time() -> bool:
        nonlocal deadline_hit
        if time.monotonic() - started > MAX_SECONDS:
            deadline_hit = True
        return deadline_hit

    sources: List[Dict[str, Any]] = []
    financial_pool: List[Dict[str, Any]] = []
    pool: List[Dict[str, Any]] = []
    post_searches = [] if scope == "financial" else read_post_searches()
    if scope != "financial" and not post_searches:
        sources.append({"kind": "post", "name": "drafts", "status": "NOT_CONFIGURED", "items": 0})

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        context = await new_stealth_context(browser)
        page = await context.new_page()
        financial_topics = await _collect_financial(page, sources, out_of_time, financial_pool) if scope in ("all", "financial") else []
        post_topics = await _collect_post_searches(page, post_searches, sources, out_of_time, pool) if scope != "financial" else []
        await browser.close()

    all_topics = [*financial_topics, *post_topics]
    homes = {**assign_unique(financial_pool, [t["external_id"] for t in financial_topics]), **assign_unique_per_post(pool, post_topics, assign_unique)}
    topics_out: List[Dict[str, Any]] = []
    await enrich_items(all_topics, homes, MAX_ITEMS_PER_TOPIC, topic_score)
    for topic_cfg in all_topics:
        collected = homes[topic_cfg["external_id"]][:MAX_ITEMS_PER_TOPIC]
        if topic_cfg.get("category") and not collected:
            continue
        for c in collected:
            cls = classify_topic_sentiment(c["text"], topic_cfg)
            c.update(sentiment=cls["sentiment"], confidence=cls["confidence"], compound=cls["compound"], crisis_flags=cls["crisis_flags"])
        analyzed = analyze_raw_x_topic({**topic_cfg, "comments": collected}, days=7)
        is_spike = (analyzed["volume_change_pct"] or 0) >= 35.0
        rec, rec_reason = evaluate_publishing_decision(
            positive_pct=analyzed["positive"], neutral_pct=analyzed["neutral"], negative_pct=analyzed["negative"],
            volume=analyzed["volume"], volume_change_pct=analyzed["volume_change_pct"], is_spike=is_spike, top_posts=analyzed["top_posts"],
        )
        topics_out.append(_topic_entry(topic_cfg, analyzed, is_spike, rec, rec_reason, collected))

    existing = await load_feed(output_path) if scope != "all" else None
    topics_out, sources = merge_scoped_feed(scope, existing, topics_out, sources)
    total_items = sum(len(t["comments"]) for t in topics_out)
    status = overall_status(sources, total_items, deadline_hit)
    payload = {
        "source": f"Playwright Live Scrape (ZenaTech Financial & Today's Posts) + sentiment models ({sentiment_models_label()})",
        "is_live_scraped": True, "collected_at": datetime.now(timezone.utc).isoformat(),
        "collection": {
            "status": status, "scope": scope, "items": total_items, "sources": sources,
            "note": "Stopped at the time limit; later sources were skipped." if deadline_hit else None,
            "limits": {"max_items_per_topic": MAX_ITEMS_PER_TOPIC, "max_posts_per_handle": MAX_POSTS_PER_HANDLE, "news_limit": NEWS_LIMIT, "max_seconds": MAX_SECONDS},
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
