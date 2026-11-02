"""Finding precedent: reviewers' verdicts on automated flags, as examples the contextual evaluator learns from.

A reviewer can mark a flag as right ("Correct flag") or as a false alarm (a dismissal, with a reason). Each call is a
labelled example: the sentence, the rule, what the check said and what the reviewer decided. The examples live in
PostgreSQL (finding_verdicts, the source of truth) and are indexed in the vector store, so that when a rule that's
judged in context meets similar wording, the closest past verdicts for that same rule are shown to the model as how
this team decided.

A verdict is guidance, never proof. The evaluator still has to quote the wording it relied on, and a hard rule (a ban or
a required disclaimer) is never cleared by precedent. Undoing a dismissal or confirmation removes its example.

Off when the vector store isn't configured: the verdicts are still saved, and are indexed when it is.
"""

import asyncio
import logging
import os
from typing import Any, Dict, List, Optional, Set
from uuid import UUID

from postgresql_db.database import fetch_all
from services.ai import vector_store_service as store

logger = logging.getLogger(__name__)

KIND = "FINDING_VERDICT"
# The most past verdicts shown for one candidate.
MAX_PRECEDENTS = 3
# How many nearest verdicts are fetched before narrowing to the candidate's own rule.
FETCH = 15
# vectorSearchScore (cosine, 0-1; 0.5 = unrelated). Below this a past verdict isn't similar enough to be shown.
MIN_SCORE = float(os.getenv("FINDING_PRECEDENT_MIN_SCORE", "0.85"))
TEXT_CHARS = 1800

_tasks: Set[asyncio.Task] = set()

_SQL = """
    SELECT v.id, v.submission_id, v.version_number, v.rule_id, v.rule_number, v.verdict, v.reason, v.note,
           v.passage, v.context, v.flagged_terms, v.finding_status, v.ai_decision, v.decided_at,
           u.full_name AS decided_by_name, s.brand_code, s.audience, s.platforms
    FROM finding_verdicts v
    JOIN content_submissions s ON s.id = v.submission_id
    LEFT JOIN users u ON u.id = v.decided_by
    WHERE s.deleted_at IS NULL {where}
"""


def _doc(row: Dict[str, Any]) -> Optional[store.VectorDoc]:
    text = (row.get("context") or row.get("passage") or "").strip()[:TEXT_CHARS]
    if not text:
        return None
    return store.VectorDoc(
        kind=KIND,
        source_id=str(row["id"]),
        title=row["rule_number"],
        text=text,
        brands=[row["brand_code"]] if row.get("brand_code") else [],
        audiences=[row["audience"]] if row.get("audience") else [],
        platforms=list(row.get("platforms") or []),
        meta={
            "rule_id": str(row["rule_id"]),
            "rule_number": row["rule_number"],
            "verdict": row["verdict"],
            "reason": row.get("reason"),
            "note": row.get("note"),
            "passage": row.get("passage"),
            "terms": list(row.get("flagged_terms") or []),
            "finding_status": row.get("finding_status"),
            "ai_decision": row.get("ai_decision"),
            "decided_by": row.get("decided_by_name"),
            "decided_at": row["decided_at"].isoformat() if row.get("decided_at") else None,
        },
    )


async def _remember(where: str = "", *args: Any) -> Dict[str, int]:
    if not store.is_enabled():
        return {"embedded": 0, "unchanged": 0}
    rows = await fetch_all(_SQL.format(where=where), *args)
    docs = [d for d in (_doc(dict(r)) for r in rows) if d]
    return await store.upsert(docs)


async def remember(verdict_id: UUID) -> Dict[str, int]:
    """Index (or refresh) one verdict."""
    return await _remember("AND v.id = $1", verdict_id)


async def remember_all() -> Dict[str, int]:
    return await _remember()


def _background(coro_factory, name: str) -> None:
    if not store.is_enabled():
        return

    async def run() -> None:
        try:
            await coro_factory()
        except Exception as e:
            logger.warning("Finding precedent: %s failed: %s", name, e)

    task = asyncio.create_task(run(), name=name)
    _tasks.add(task)
    task.add_done_callback(_tasks.discard)


def remember_soon(verdict_id: UUID) -> None:
    """remember() in the background, so the request that triggered it doesn't wait on embedding."""
    _background(lambda: remember(verdict_id), f"finding-precedent-{verdict_id}")


def forget_soon(verdict_id: UUID) -> None:
    """Take a verdict back out of the index (it was undone)."""
    _background(lambda: store.delete([f"{KIND}:{verdict_id}"]), f"finding-precedent-forget-{verdict_id}")


async def remember_all_in_background() -> None:
    """Startup: index verdicts given while the vector store was off or the server was down."""
    if not store.is_enabled():
        return
    try:
        logger.info("Finding precedent up to date: %s", await remember_all())
    except Exception as e:
        logger.warning("Couldn't bring finding precedent up to date: %s", e)


async def similar(rule_id: UUID, text: str, brand: Optional[str] = None) -> List[Dict[str, Any]]:
    """Past verdicts on this rule for wording like `text`, closest first, as {score, verdict, reason, note, passage}.
    Empty when the store is off or nothing is close enough."""
    if not store.is_enabled() or not text.strip():
        return []
    rows = await store.search(text[:TEXT_CHARS], kinds=[KIND], limit=FETCH, min_score=MIN_SCORE)
    out: List[Dict[str, Any]] = []
    for r in rows:
        meta = r.get("meta") or {}
        if meta.get("rule_id") != str(rule_id):
            continue
        out.append({
            "id": r.get("source_id"),
            "score": round(float(r.get("score", 0)), 3),
            "verdict": meta.get("verdict"),
            "reason": meta.get("reason"),
            "note": meta.get("note"),
            "passage": meta.get("passage") or r.get("text"),
            "finding_status": meta.get("finding_status"),
        })
        if len(out) >= MAX_PRECEDENTS:
            break
    return out
