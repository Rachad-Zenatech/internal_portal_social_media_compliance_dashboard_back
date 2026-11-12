"""Policy watch: keeps the platform content-policy norms (Messaging Context entries seeded as "policy:...") up to date.

Once a week every norm's official policy pages (the links in its source) are read again. When a page's text changed
in a way that matters, Gemini compares the old and new text with the norm and drafts an updated norm, saying what
changed. A super admin approves it (with their own edits if they like), which replaces the norm's wording, or rejects
it. Nothing changes a norm on its own, and nobody but super admins sees or decides on the drafts.

The first read of a page only stores it (there is nothing to compare with yet).

    run_check / start      read every page; draft an update for a norm whose pages changed
    policy_watch_loop      runs the check weekly (POLICY_WATCH_DAYS, default 7)
    approve / reject       a super admin's decision on a draft
"""

import asyncio
import hashlib
import json
import logging
import os
import re
from datetime import date, datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple
from uuid import UUID

import httpx
from fastapi import HTTPException
from google import genai
from google.genai import types

from models.compliance.context_model import PolicyPageStatus, PolicyProposal, PolicyProposalApprove, PolicyWatch
from postgresql_db.database import fetch_all, fetch_one, get_pool
from services.ai.core.gemini_service import generate_content_with_fallback
from services.compliance.evaluation import newsroom_sync_service, reference_index_service
from services.compliance.evaluation.context_service import POLICY_SEED_PREFIX
from services.shared.core.audit_service import log_audit_event

logger = logging.getLogger(__name__)

SOURCE = "POLICY_PAGES"
CHECK_EVERY = timedelta(days=int(os.getenv("POLICY_WATCH_DAYS", "7")))
USER_AGENT = newsroom_sync_service.USER_AGENT
# Long policy hubs are cut here; what matters for a norm is near the top and in the headings.
MAX_PAGE_CHARS = 60_000
# Less than this much text added or removed is noise (dates, counters, cookie banners), not a policy change.
MIN_CHANGED_CHARS = 200
# A page with less text than this only fills in with JavaScript in a browser: there's nothing to compare.
MIN_PAGE_CHARS = 500
NEEDS_BROWSER = ("The page shows almost no text unless it's opened in a browser (it loads with JavaScript), so changes "
                 "to it can't be spotted. Check it by hand now and then.")
# What the AI is shown of a change.
MAX_DIFF_CHARS = 12_000
_URL = re.compile(r"https?://[^\s·,]+")

PROMPT = """You keep one short "platform norm" up to date. A compliance reviewer at ZenaTech (a technology company that
posts about drones, AI and its businesses) uses it to judge whether a social media post follows {title}.

The platform's official policy page(s) below changed. Decide whether the change affects what this norm should tell
someone writing a company post (disclosure of paid content, misleading claims, music and copyright, AI content
labels, financial or investment content, dangerous acts, privacy, spam...). Changes to navigation, dates, layout or
topics irrelevant to company posts do not affect it.

If it affects the norm, rewrite the norm: keep its style and length (body at most 900 characters, examples at most 400
each), change only what the policy change requires, and keep everything else as it is. Then list what changed in
short plain points (what the policy now says, and what you changed in the norm). Never invent rules that are not in
the policy text.

CURRENT NORM
Body: {body}
Do: {do}
Don't: {dont}

WHAT CHANGED ON THE POLICY PAGE(S)
{diff}
"""

RESPONSE_SCHEMA = {
    "type": "OBJECT",
    "properties": {
        "affects_norm": {"type": "BOOLEAN"},
        "changes": {"type": "ARRAY", "items": {"type": "STRING"}},
        "body": {"type": "STRING"},
        "do_examples": {"type": "STRING"},
        "dont_examples": {"type": "STRING"},
    },
    "required": ["affects_norm", "changes"],
}

_client: Optional[genai.Client] = None
_task: Optional["asyncio.Task[None]"] = None


def _get_client() -> genai.Client:
    global _client
    if _client is None:
        api_key = os.getenv("GEMINI_API_KEY")
        if not api_key:
            raise RuntimeError("GEMINI_API_KEY is not configured, so policy changes can't be drafted.")
        _client = genai.Client(api_key=api_key)
    return _client


# ---------------------------------------------------------------------------
# Reading pages and spotting changes
# ---------------------------------------------------------------------------

def page_text(page: str) -> str:
    """A policy page as plain text: one line per paragraph, scripts and styles dropped, spacing normalised."""
    return newsroom_sync_service._text(page)[:MAX_PAGE_CHARS]


def text_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def urls_of(source: Optional[str]) -> List[str]:
    """The policy pages a norm names in its source (one or more links)."""
    return list(dict.fromkeys(u.rstrip(".") for u in _URL.findall(source or "")))


def diff(old: str, new: str) -> Optional[str]:
    """The lines removed and added between two versions of a page, or None when the change is only noise."""
    old_lines, new_lines = old.splitlines(), new.splitlines()
    old_set, new_set = set(old_lines), set(new_lines)
    removed = [line for line in old_lines if line not in new_set]
    added = [line for line in new_lines if line not in old_set]
    if sum(len(line) for line in removed + added) < MIN_CHANGED_CHARS:
        return None
    out = "REMOVED:\n" + "\n".join(f"- {line}" for line in removed) + "\n\nADDED:\n" + "\n".join(f"+ {line}" for line in added)
    return out[:MAX_DIFF_CHARS]


async def _draft(entry: Dict[str, Any], changes: str) -> Optional[Dict[str, Any]]:
    """Gemini's updated norm for these page changes, or None when they don't affect the norm."""
    prompt = PROMPT.format(title=entry["title"], body=entry["body"], do=entry["do_examples"] or "-",
                           dont=entry["dont_examples"] or "-", diff=changes)
    config = types.GenerateContentConfig(response_mime_type="application/json", response_schema=RESPONSE_SCHEMA,
                                         temperature=0.1)
    response = await asyncio.wait_for(
        generate_content_with_fallback(_get_client(), prompt, config, feature="POLICY_WATCH"), timeout=180
    )
    data = json.loads(response.text or "{}")
    body = (data.get("body") or "").strip()
    if not data.get("affects_norm") or not body:
        return None
    return {
        "changes": [str(c).strip()[:300] for c in (data.get("changes") or []) if str(c).strip()][:10],
        "body": body[:2000],
        "do_examples": (data.get("do_examples") or "").strip()[:1000] or None,
        "dont_examples": (data.get("dont_examples") or "").strip()[:1000] or None,
    }


async def _watched_norms() -> List[Dict[str, Any]]:
    return await fetch_all(
        "SELECT id, title, body, do_examples, dont_examples, source, platforms FROM messaging_context "
        "WHERE seed_key LIKE $1 AND deleted_at IS NULL ORDER BY title",
        POLICY_SEED_PREFIX + "%",
    )


async def _read(http: httpx.AsyncClient, url: str, counts: Dict[str, int]) -> Optional[str]:
    """Read one page and store it. Returns what changed since the last read (None: first read, same, or noise)."""
    before = await fetch_one("SELECT text_hash, page_text FROM policy_norm_pages WHERE url = $1", url)
    try:
        response = await http.get(url)
        response.raise_for_status()
        text = page_text(response.text)
        if len(text) < MIN_PAGE_CHARS:
            raise ValueError(NEEDS_BROWSER)
    except Exception as e:  # One unreadable page doesn't stop the rest.
        counts["failed"] += 1
        logger.warning("Policy watch: couldn't read %s: %s", url, e)
        if isinstance(e, httpx.HTTPStatusError):
            reason = f"The site refused to send the page (HTTP {e.response.status_code}). Check it by hand now and then."
        else:
            reason = str(e)
        # What was last read is kept, so a page that comes back is compared with that.
        await fetch_one(
            "INSERT INTO policy_norm_pages (url, fetched_at, error) VALUES ($1, NOW(), $2) "
            "ON CONFLICT (url) DO UPDATE SET fetched_at = NOW(), error = EXCLUDED.error RETURNING url",
            url, reason[:500],
        )
        return None
    new_hash = text_hash(text)
    changed = diff(before["page_text"] or "", text) if before and before["page_text"] and before["text_hash"] != new_hash else None
    await fetch_one(
        """
        INSERT INTO policy_norm_pages (url, text_hash, page_text, fetched_at, changed_at, error)
        VALUES ($1, $2, $3, NOW(), $4, NULL)
        ON CONFLICT (url) DO UPDATE SET text_hash = EXCLUDED.text_hash, page_text = EXCLUDED.page_text,
            fetched_at = NOW(), changed_at = COALESCE(EXCLUDED.changed_at, policy_norm_pages.changed_at), error = NULL
        RETURNING url
        """,
        url, new_hash, text, datetime.now(timezone.utc) if changed else None,
    )
    if not before or not before["page_text"]:
        counts["added"] += 1  # first read: kept to compare with next time
    elif not changed:
        counts["unchanged"] += 1
    return changed


async def _propose(entry: Dict[str, Any], urls: List[str], draft: Dict[str, Any]) -> None:
    """Save a drafted update, replacing an older one still waiting for this norm."""
    pool = get_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            await conn.execute(
                "UPDATE policy_norm_proposals SET status = 'SUPERSEDED', decided_at = NOW() "
                "WHERE context_id = $1 AND status = 'PENDING'",
                entry["id"],
            )
            await conn.execute(
                """
                INSERT INTO policy_norm_proposals (context_id, source_urls, changes, current_body, current_do_examples,
                    current_dont_examples, body, do_examples, dont_examples)
                VALUES ($1, $2, $3::jsonb, $4, $5, $6, $7, $8, $9)
                """,
                entry["id"], urls, json.dumps(draft["changes"]), entry["body"], entry["do_examples"],
                entry["dont_examples"], draft["body"], draft["do_examples"], draft["dont_examples"],
            )


async def _record(run_id: UUID, **fields: Any) -> None:
    sets = ", ".join(f"{k} = ${i + 2}" for i, k in enumerate(fields))
    await fetch_one(f"UPDATE messaging_context_syncs SET {sets} WHERE id = $1 RETURNING id", run_id, *fields.values())


async def run_check(run_id: UUID) -> None:
    """Read every watched page; draft an update for each norm whose pages changed in a way that matters.
    Counts: found = pages, added = first reads, unchanged, updated = drafts made, skipped = changes that don't affect
    the norm, failed = pages that couldn't be read or drafts that couldn't be made."""
    counts = {"added": 0, "updated": 0, "unchanged": 0, "skipped": 0, "failed": 0}
    try:
        norms = await _watched_norms()
        await _record(run_id, found=sum(len(urls_of(n["source"])) for n in norms))
        async with httpx.AsyncClient(timeout=30, follow_redirects=True, headers={"User-Agent": USER_AGENT}) as http:
            for norm in norms:
                urls = urls_of(norm["source"])
                changes: List[Tuple[str, str]] = []
                for url in urls:
                    changed = await _read(http, url, counts)
                    if changed:
                        changes.append((url, changed))
                if not changes:
                    continue
                try:
                    text = "\n\n".join(f"PAGE: {url}\n{d}" for url, d in changes)[:MAX_DIFF_CHARS]
                    draft = await _draft(norm, text)
                    if draft:
                        await _propose(norm, [u for u, _ in changes], draft)
                        counts["updated"] += 1
                    else:
                        counts["skipped"] += len(changes)
                except Exception as e:
                    counts["failed"] += 1
                    logger.warning("Policy watch: couldn't draft an update for %s: %s", norm["title"], e)
                await _record(run_id, **counts)
        await _record(run_id, status="DONE", finished_at=datetime.now(timezone.utc), **counts)
        logger.info("Policy watch done: %s", counts)
    except Exception as e:
        logger.exception("Policy watch failed")
        await _record(run_id, status="FAILED", finished_at=datetime.now(timezone.utc), error=str(e)[:500], **counts)


# ---------------------------------------------------------------------------
# Running it
# ---------------------------------------------------------------------------

async def is_super_admin(user_id: UUID) -> bool:
    return bool(await fetch_one("SELECT 1 FROM users WHERE id = $1 AND is_super_admin AND is_active", user_id))


async def require_super_admin(user_id: UUID) -> None:
    if not await is_super_admin(user_id):
        raise HTTPException(status_code=403, detail="Only a super admin can review platform content policy updates")


async def _latest() -> Optional[Dict[str, Any]]:
    return newsroom_sync_service._out(await fetch_one(
        """
        SELECT s.*, u.full_name AS started_by_name FROM messaging_context_syncs s
        LEFT JOIN users u ON u.id = s.started_by
        WHERE s.source = $1 ORDER BY s.started_at DESC LIMIT 1
        """,
        SOURCE,
    ))


async def start(trigger: str, user_id: Optional[UUID] = None) -> Optional[Dict[str, Any]]:
    """Start a check unless one is already running (a run left RUNNING for over an hour is treated as dead)."""
    global _task
    pool = get_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            await conn.execute("SELECT pg_advisory_xact_lock(hashtext('messaging_context_policy_watch'))")
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
        _task = asyncio.create_task(run_check(run_id), name="policy-watch")
    return await _latest()


async def policy_watch_loop() -> None:
    """Weekly: check when the last finished check is older than CHECK_EVERY (looked at hourly, so restarts don't skip
    a week). A check that failed or was cut off is tried again an hour later."""
    await fetch_one(
        "UPDATE messaging_context_syncs SET status = 'FAILED', error = 'Stopped when the server restarted', "
        "finished_at = NOW() WHERE source = $1 AND status = 'RUNNING' RETURNING id",
        SOURCE,
    )
    await asyncio.sleep(90)
    while True:
        try:
            last = await fetch_one(
                "SELECT max(started_at) FILTER (WHERE status = 'DONE') AS done_at, max(started_at) AS tried_at "
                "FROM messaging_context_syncs WHERE source = $1",
                SOURCE,
            )
            now = datetime.now(timezone.utc)
            due = not last or not last["done_at"] or now - last["done_at"] >= CHECK_EVERY
            rested = not last or not last["tried_at"] or now - last["tried_at"] >= timedelta(hours=1)
            if due and rested:
                await start("SCHEDULED")
        except Exception:
            logger.exception("Policy watch scheduling failed")
        await asyncio.sleep(3600)


# ---------------------------------------------------------------------------
# Reviewing drafts (super admins)
# ---------------------------------------------------------------------------

_PROPOSAL_SELECT = """
    SELECT p.*, m.title AS norm_title, m.platforms FROM policy_norm_proposals p
    JOIN messaging_context m ON m.id = p.context_id
"""


def _proposal(r: Dict[str, Any]) -> PolicyProposal:
    changes = r["changes"]
    if isinstance(changes, str):
        changes = json.loads(changes)
    return PolicyProposal(
        id=r["id"], context_id=r["context_id"], norm_title=r["norm_title"], platforms=list(r["platforms"] or []),
        source_urls=list(r["source_urls"] or []), changes=list(changes or []), current_body=r["current_body"],
        current_do_examples=r["current_do_examples"], current_dont_examples=r["current_dont_examples"],
        body=r["body"], do_examples=r["do_examples"], dont_examples=r["dont_examples"], status=r["status"],
        created_at=r["created_at"],
    )


async def overview(user_id: UUID) -> PolicyWatch:
    await require_super_admin(user_id)
    rows = await fetch_all(_PROPOSAL_SELECT + " WHERE p.status = 'PENDING' AND m.deleted_at IS NULL ORDER BY p.created_at DESC")
    pages: List[PolicyPageStatus] = []
    for norm in await _watched_norms():
        urls = urls_of(norm["source"])
        stored = {r["url"]: r for r in await fetch_all(
            "SELECT url, fetched_at, changed_at, error, page_text IS NOT NULL AS has_text FROM policy_norm_pages "
            "WHERE url = ANY($1::text[])", urls,
        )}
        for url in urls:
            r = stored.get(url)
            pages.append(PolicyPageStatus(
                url=url, norm_title=norm["title"], fetched_at=r["fetched_at"] if r else None,
                changed_at=r["changed_at"] if r else None, error=r["error"] if r else None,
                watching=bool(r and r["has_text"] and not r["error"]),
            ))
    return PolicyWatch(last_check=await _latest(), proposals=[_proposal(r) for r in rows],
                       watched_pages=sum(p.watching for p in pages), pages=pages)


async def check_now(user_id: UUID) -> PolicyWatch:
    await require_super_admin(user_id)
    await start("MANUAL", user_id)
    return await overview(user_id)


async def _pending(proposal_id: UUID) -> Dict[str, Any]:
    row = await fetch_one(_PROPOSAL_SELECT + " WHERE p.id = $1", proposal_id)
    if not row:
        raise HTTPException(status_code=404, detail="Policy update not found")
    if row["status"] != "PENDING":
        raise HTTPException(status_code=409, detail="This update was already decided or replaced by a newer one")
    return row


async def approve(proposal_id: UUID, user_id: UUID, edits: Optional[PolicyProposalApprove] = None) -> PolicyWatch:
    """Replace the norm's wording with the drafted update (or the super admin's edited version of it)."""
    await require_super_admin(user_id)
    row = await _pending(proposal_id)
    body = (edits.body if edits and edits.body else row["body"]).strip()
    do_examples = edits.do_examples if edits and edits.do_examples is not None else row["do_examples"]
    dont_examples = edits.dont_examples if edits and edits.dont_examples is not None else row["dont_examples"]
    pool = get_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            updated = await conn.fetchval(
                "UPDATE messaging_context SET body = $2, do_examples = $3, dont_examples = $4, as_of = $5, "
                "updated_by = $6, updated_at = NOW() WHERE id = $1 AND deleted_at IS NULL RETURNING id",
                row["context_id"], body, do_examples or None, dont_examples or None, date.today(), user_id,
            )
            if not updated:
                raise HTTPException(status_code=404, detail="The norm this update is for was removed")
            await conn.execute(
                "UPDATE policy_norm_proposals SET status = 'APPROVED', decided_at = NOW(), decided_by = $2 WHERE id = $1",
                proposal_id, user_id,
            )
    await log_audit_event("messaging_context", str(row["context_id"]), "APPROVE_POLICY_UPDATE", user_id, {
        "proposal_id": str(proposal_id), "changes": _proposal(row).changes,
        "before": {"body": row["current_body"], "do": row["current_do_examples"], "dont": row["current_dont_examples"]},
        "after": {"body": body, "do": do_examples, "dont": dont_examples},
    })
    reference_index_service.sync_soon()
    return await overview(user_id)


async def reject(proposal_id: UUID, user_id: UUID) -> PolicyWatch:
    await require_super_admin(user_id)
    await _pending(proposal_id)
    await fetch_one(
        "UPDATE policy_norm_proposals SET status = 'REJECTED', decided_at = NOW(), decided_by = $2 WHERE id = $1 RETURNING id",
        proposal_id, user_id,
    )
    await log_audit_event("policy_norm_proposals", str(proposal_id), "REJECT_POLICY_UPDATE", user_id, {})
    return await overview(user_id)
