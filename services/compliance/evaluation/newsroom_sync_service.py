"""Newsroom sync: the ZenaTech newsroom (https://www.zenatech.com/newsroom/) as Messaging Context facts.

Every article becomes one FACT: a short, dated statement of what the release says is true (products, their status,
partners, contracts, numbers), so the analysis can tell whether a post's claims are plausible. Each sync reads the list
and every article on it; a new article is added, a changed one is re-read and its fact updated. A fact someone edited
keeps their wording, and one someone removed is not added back.

Runs once a day in the background (newsroom_sync_loop) and on demand from the Messaging Context page.
"""

import asyncio
import hashlib
import html
import json
import logging
import os
import re
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional
from uuid import UUID

import httpx
from google import genai
from google.genai import types

from postgresql_db.database import fetch_all, fetch_one, get_pool
from services.ai.core.gemini_service import generate_content_with_fallback
from services.compliance.evaluation import reference_index_service

logger = logging.getLogger(__name__)

SOURCE = "NEWSROOM"
NEWSROOM_URL = os.getenv("NEWSROOM_URL", "https://www.zenatech.com/newsroom/")
SYNC_EVERY = timedelta(hours=24)
# Be polite to the site and the AI quota.
FETCH_CONCURRENCY = 4
AI_CONCURRENCY = 3
MAX_ARTICLE_CHARS = 12_000
USER_AGENT = "Mozilla/5.0 (compatible; ZenaTechCompliancePortal/1.0; +https://www.zenatech.com/)"

_client: Optional[genai.Client] = None
_task: Optional[asyncio.Task] = None

_ITEM = re.compile(
    r'<a href="(?P<url>https://www\.zenatech\.com/[^"]+)" class="post-item">.*?<div class="post-right">.*?'
    r"<h3>(?P<title>.*?)</h3>.*?</span>\s*(?P<date>\d{1,2} [A-Za-z]+ \d{4})</p>",
    re.S,
)

PROMPT = """You turn a ZenaTech press release into one entry for a fact sheet that a compliance reviewer uses to judge
whether social media posts make plausible claims.

Write:
- title: what the release is about, in at most 12 words (e.g. "ZenaDrone 2000 interceptor moving toward flight testing").
- fact: 1-3 plain sentences, at most 450 characters, saying what is true as of the release date: which products,
  services, companies or partners; their status exactly as stated (shipping, in trials, in development, planned,
  expected, signed offer, acquisition completed, certification applied for vs granted); places, contract values and
  counts. Keep the release's own hedging: "plans to", "expects", "aims to" stay as plans, never as done. No marketing
  adjectives, no quotes, no forward-looking promises stated as fact, no stock-price commentary.

Release date: {date}
Release title: {title}

Release text:
{text}"""

RESPONSE_SCHEMA: Dict[str, Any] = {
    "type": "OBJECT",
    "properties": {"title": {"type": "STRING"}, "fact": {"type": "STRING"}},
    "required": ["title", "fact"],
}


def _get_client() -> genai.Client:
    global _client
    if _client is None:
        api_key = os.getenv("GEMINI_API_KEY")
        if not api_key:
            raise RuntimeError("GEMINI_API_KEY is not configured, so the newsroom can't be read.")
        _client = genai.Client(api_key=api_key)
    return _client


def _text(fragment: str) -> str:
    fragment = re.sub(r"<(script|style)\b.*?</\1>", " ", fragment, flags=re.S | re.I)
    fragment = re.sub(r"<br\s*/?>|</p>|</li>|</h\d>", "\n", fragment, flags=re.I)
    fragment = html.unescape(re.sub(r"<[^>]+>", " ", fragment))
    lines = (re.sub(r"[ \t ]+", " ", line).strip() for line in fragment.splitlines())
    return "\n".join(line for line in lines if line)


def parse_list(page: str) -> List[Dict[str, Any]]:
    """The articles on the newsroom page: url, title and date, newest first, one per url."""
    seen, items = set(), []
    for m in _ITEM.finditer(page):
        url = m.group("url")
        if url in seen:
            continue
        seen.add(url)
        try:
            published = datetime.strptime(m.group("date"), "%d %B %Y").date()
        except ValueError:
            published = None
        items.append({"url": url, "title": _text(m.group("title")), "date": published})
    return items


def article_text(page: str) -> str:
    """The release itself: from its headline to the boilerplate ("About ZenaTech", safe harbor). The headline follows
    the site's language switcher ("Español"); the release opens with the company's name and ticker."""
    body = page.find("<body")
    full = _text(page[max(body, 0):])
    opening = next((i for i in (full.find("(Nasdaq: ZENA)"), full.find("ZenaTech, Inc.")) if i >= 0), -1)
    if opening < 0:
        return ""
    switcher = full.rfind("Español", 0, opening)
    start = switcher + len("Español") if switcher >= 0 and opening - switcher < 2000 else max(0, opening - 600)
    end = len(full)
    for marker in ("About ZenaTech", "Safe Harbor", "Forward-looking", "Forward-Looking"):
        i = full.find(marker, opening)
        if 0 <= i < end:
            end = i
    return full[start:end].strip()[:MAX_ARTICLE_CHARS]


async def _summarize(item: Dict[str, Any], text: str) -> Dict[str, str]:
    prompt = PROMPT.format(date=item["date"] or "unknown", title=item["title"], text=text)
    config = types.GenerateContentConfig(
        response_mime_type="application/json", response_schema=RESPONSE_SCHEMA, temperature=0.1
    )
    response = await generate_content_with_fallback(_get_client(), prompt, config, feature="NEWSROOM")
    data = json.loads(response.text or "{}")
    title = (data.get("title") or item["title"]).strip()[:200]
    fact = (data.get("fact") or "").strip()[:2000]
    if not fact:
        raise ValueError("empty fact")
    return {"title": title, "fact": fact}


async def _save(item: Dict[str, Any], text: str, existing: Optional[Dict[str, Any]]) -> str:
    """Add or update the fact for one article. Returns added / updated / unchanged / skipped."""
    content_hash = hashlib.sha256(text.encode("utf-8")).hexdigest()
    if existing:
        if existing["deleted_at"] is not None:
            return "skipped"  # Someone removed it: leave it removed.
        if existing["content_hash"] == content_hash:
            return "unchanged"
    summary = await _summarize(item, text)
    source = item["url"] if len(item["url"]) <= 300 else "ZenaTech Newsroom"
    if existing is None:
        await fetch_one(
            """
            INSERT INTO messaging_context (kind, title, body, source, source_url, as_of, auto_source, auto_body, content_hash)
            VALUES ('FACT', $1, $2, $3, $4, $5, $6, $2, $7)
            ON CONFLICT (source_url) DO NOTHING
            RETURNING id
            """,
            summary["title"], summary["fact"], source, item["url"], item["date"], SOURCE, content_hash,
        )
        return "added"
    # The article changed. A fact someone rewrote keeps their wording; otherwise it takes the new reading.
    hand_edited = existing["body"] != existing["auto_body"]
    await fetch_one(
        """
        UPDATE messaging_context
        SET title = CASE WHEN $2 THEN title ELSE $3 END,
            body = CASE WHEN $2 THEN body ELSE $4 END,
            as_of = coalesce($5, as_of), auto_body = $4, content_hash = $6, updated_at = NOW()
        WHERE id = $1 RETURNING id
        """,
        existing["id"], hand_edited, summary["title"], summary["fact"], item["date"], content_hash,
    )
    return "updated"


async def _record(run_id: UUID, **fields: Any) -> None:
    sets = ", ".join(f"{k} = ${i + 2}" for i, k in enumerate(fields))
    await fetch_one(f"UPDATE messaging_context_syncs SET {sets} WHERE id = $1 RETURNING id", run_id, *fields.values())


async def run_sync(run_id: UUID) -> None:
    counts = {"added": 0, "updated": 0, "unchanged": 0, "skipped": 0, "failed": 0}
    try:
        async with httpx.AsyncClient(timeout=30, follow_redirects=True, headers={"User-Agent": USER_AGENT}) as http:
            listing = await http.get(NEWSROOM_URL)
            listing.raise_for_status()
            items = parse_list(listing.text)
            if not items:
                raise RuntimeError("No articles found on the newsroom page; its layout may have changed.")
            await _record(run_id, found=len(items))
            rows = await fetch_all(
                "SELECT id, body, auto_body, content_hash, deleted_at, source_url FROM messaging_context "
                "WHERE source_url = ANY($1::text[])",
                [i["url"] for i in items],
            )
            existing = {r["source_url"]: r for r in rows}
            fetch_gate, ai_gate = asyncio.Semaphore(FETCH_CONCURRENCY), asyncio.Semaphore(AI_CONCURRENCY)

            async def one(item: Dict[str, Any]) -> None:
                row = existing.get(item["url"])
                if row and row["deleted_at"] is not None:
                    counts["skipped"] += 1
                    return
                try:
                    async with fetch_gate:
                        page = await http.get(item["url"])
                        page.raise_for_status()
                    text = article_text(page.text)
                    if not text:
                        raise ValueError("no article text")
                    async with ai_gate:
                        outcome = await _save(item, text, row)
                    counts[outcome] += 1
                except Exception as e:  # One bad article doesn't stop the rest.
                    counts["failed"] += 1
                    logger.warning("Newsroom sync: couldn't read %s: %s", item["url"], e)
                if sum(counts.values()) % 10 == 0:
                    await _record(run_id, **counts)

            await asyncio.gather(*(one(i) for i in items))
        await _record(run_id, status="DONE", finished_at=datetime.now(timezone.utc), **counts)
        logger.info("Newsroom sync done: %s", counts)
    except Exception as e:
        logger.exception("Newsroom sync failed")
        await _record(run_id, status="FAILED", finished_at=datetime.now(timezone.utc), error=str(e)[:500], **counts)
    finally:
        if counts["added"] or counts["updated"]:
            reference_index_service.sync_soon()


def _out(r: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    if not r:
        return None
    keys = ("id", "status", "trigger", "started_at", "finished_at", "found", "added", "updated", "unchanged",
            "skipped", "failed", "error", "started_by_name")
    return {k: r.get(k) for k in keys}


async def latest() -> Optional[Dict[str, Any]]:
    return _out(await fetch_one(
        """
        SELECT s.*, u.full_name AS started_by_name FROM messaging_context_syncs s
        LEFT JOIN users u ON u.id = s.started_by
        WHERE s.source = $1 ORDER BY s.started_at DESC LIMIT 1
        """,
        SOURCE,
    ))


async def start(trigger: str, user_id: Optional[UUID] = None) -> Dict[str, Any]:
    """Start a sync unless one is already running (a run left RUNNING for over an hour is treated as dead)."""
    global _task
    pool = get_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            await conn.execute("SELECT pg_advisory_xact_lock(hashtext('messaging_context_newsroom_sync'))")
            await conn.execute(
                "UPDATE messaging_context_syncs SET status = 'FAILED', error = 'Stopped before it finished', "
                "finished_at = NOW() WHERE source = $1 AND status = 'RUNNING' AND started_at < NOW() - INTERVAL '1 hour'",
                SOURCE,
            )
            running = await conn.fetchval(
                "SELECT id FROM messaging_context_syncs WHERE source = $1 AND status = 'RUNNING'", SOURCE
            )
            if running is None:
                run_id = await conn.fetchval(
                    "INSERT INTO messaging_context_syncs (source, trigger, started_by) VALUES ($1, $2, $3) RETURNING id",
                    SOURCE, trigger, user_id,
                )
    if running is None:
        _task = asyncio.create_task(run_sync(run_id), name="newsroom-sync")
    return await latest()  # type: ignore[return-value]


async def newsroom_sync_loop() -> None:
    """Once a day: sync when the last finished sync is over a day old (checked hourly, so restarts don't skip a day)."""
    await asyncio.sleep(60)
    while True:
        try:
            last = await fetch_one(
                "SELECT max(started_at) AS at FROM messaging_context_syncs WHERE source = $1 AND status IN ('DONE', 'RUNNING')",
                SOURCE,
            )
            if not last or not last["at"] or datetime.now(timezone.utc) - last["at"] >= SYNC_EVERY:
                await start("SCHEDULED")
        except Exception:
            logger.exception("Newsroom sync scheduling failed")
        await asyncio.sleep(3600)

