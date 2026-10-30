"""Review memory: this team's past review decisions, used as precedent when new content is analysed.

Every decided review (approved, or sent back for changes) is kept in the vector store with the content that was
reviewed and what reviewers said about it: the decision and its note, their comments, compliance flags they
dismissed as false alarms, and sentiment findings they rated useful or "not an issue". When a post is analysed,
the past reviews closest to it in meaning are given to Gemini as precedent, so it judges new content the way this
team has judged similar content. That matters most for repeated campaigns and recurring wording.

Off when the vector store isn't configured (MONGODB_URI): nothing is stored and analyses get no precedent.

Kept up to date by remember_soon() after a decision, a reopened approval or a finding rating, and by
remember_all() at startup (which embeds only what's new or changed, so it's cheap to repeat).
"""

import asyncio
import json
import logging
import os
from typing import Any, Dict, List, Optional, Set
from uuid import UUID

from postgresql_db.database import fetch_all
from services.ai import vector_store_service as store

logger = logging.getLogger(__name__)

KIND = "REVIEW_DECISION"
MAX_PRECEDENTS = 4
# vectorSearchScore for cosine similarity runs 0-1 (0.5 = unrelated). Below this, a past review isn't similar
# enough to be precedent.
MIN_SCORE = float(os.getenv("REVIEW_MEMORY_MIN_SCORE", "0.88"))
TEXT_CHARS = 4000
DECISION_LABELS = {"APPROVE": "APPROVED", "REQUEST_CHANGES": "CHANGES REQUESTED"}

_tasks: Set[asyncio.Task] = set()

# The latest decision on each reviewed version, with what reviewers said about it. {where} narrows it down.
_DECISIONS_SQL = """
    SELECT DISTINCT ON (j.submission_id, j.version_number)
           j.submission_id, j.version_number, j.decision, j.decision_notes, j.decided_at,
           s.reference_code, v.snapshot, u.full_name AS decided_by_name, t.transcript,
           COALESCE((
               SELECT json_agg(c.body ORDER BY c.created_at) FROM content_review_comments c
               WHERE c.submission_id = j.submission_id AND c.version_number = j.version_number
                 AND c.deleted_at IS NULL AND c.from_reviewer IS NOT FALSE
           ), '[]') AS comments,
           COALESCE((
               SELECT json_agg(json_build_object('rule_number', fr.rule_number, 'terms', fr.flagged_terms,
                                                 'reason', fr.reason, 'note', fr.note))
               FROM flag_reviews fr WHERE fr.submission_id = j.submission_id AND fr.version_number = j.version_number
           ), '[]') AS dismissed_flags,
           COALESCE((
               SELECT json_agg(DISTINCT jsonb_build_object('verdict', fb.verdict, 'quote', fb.quote))
               FROM content_sentiment_feedback fb JOIN content_sentiment_analyses a ON a.id = fb.analysis_id
               WHERE a.submission_id = j.submission_id AND a.version_number = j.version_number AND fb.quote IS NOT NULL
           ), '[]') AS ratings
    FROM content_review_jobs j
    JOIN content_submissions s ON s.id = j.submission_id
    JOIN content_submission_versions v ON v.submission_id = j.submission_id AND v.version_number = j.version_number
    LEFT JOIN users u ON u.id = j.decided_by
    LEFT JOIN LATERAL (
        SELECT f.transcript FROM content_submission_files f
        WHERE f.id::text IN (SELECT e->>'id' FROM jsonb_array_elements(COALESCE(v.snapshot->'files', '[]'::jsonb)) e
                             WHERE e->>'kind' = 'MAIN')
          AND f.mime_type LIKE 'video/%' AND f.transcript IS NOT NULL
        LIMIT 1
    ) t ON true
    WHERE j.decision IS NOT NULL AND s.deleted_at IS NULL {where}
    ORDER BY j.submission_id, j.version_number, j.decided_at DESC NULLS LAST
"""


def _json(value: Any) -> Any:
    return json.loads(value) if isinstance(value, str) else value


def _doc(row: Dict[str, Any]) -> Optional[store.VectorDoc]:
    """A reviewed version as a vector document: its content is what gets matched, the review is what comes back."""
    snap = _json(row["snapshot"]) or {}
    transcript = " ".join(s.get("text", "") for s in (_json(row["transcript"]) or []) if isinstance(s, dict))
    text = "\n".join(p for p in ((snap.get("caption_text") or "").strip(), transcript) if p.strip())[:TEXT_CHARS]
    title = snap.get("title") or ""
    if not (title.strip() or text.strip()):
        return None
    return store.VectorDoc(
        kind=KIND,
        source_id=f"{row['submission_id']}:{row['version_number']}",
        title=title,
        text=text,
        brands=[snap["brand_code"]] if snap.get("brand_code") else [],
        audiences=[snap["audience"]] if snap.get("audience") else [],
        platforms=list(snap.get("platforms") or []),
        meta={
            "submission_id": str(row["submission_id"]),
            "reference_code": row["reference_code"],
            "version": row["version_number"],
            "decision": row["decision"],
            "notes": row["decision_notes"],
            "decided_at": row["decided_at"].isoformat() if row["decided_at"] else None,
            "decided_by": row["decided_by_name"],
            "content_type": snap.get("content_type"),
            "brand": snap.get("brand_code"),
            "comments": [c for c in (_json(row["comments"]) or []) if c][:8],
            "dismissed_flags": (_json(row["dismissed_flags"]) or [])[:8],
            "ratings": (_json(row["ratings"]) or [])[:12],
        },
    )


async def _remember(where: str = "", *args: Any) -> Dict[str, int]:
    if not store.is_enabled():
        return {"embedded": 0, "unchanged": 0}
    rows = await fetch_all(_DECISIONS_SQL.format(where=where), *args)
    docs = [d for d in (_doc(dict(r)) for r in rows) if d]
    return await store.upsert(docs)


async def remember(submission_id: UUID, version: int) -> Dict[str, int]:
    """Store (or refresh) one reviewed version. Nothing happens if it hasn't been decided yet."""
    return await _remember("AND j.submission_id = $1 AND j.version_number = $2", submission_id, version)


async def remember_all() -> Dict[str, int]:
    """Store every decided review; only new or changed content is embedded again."""
    return await _remember()


def remember_soon(submission_id: UUID, version: int) -> None:
    """remember() in the background, so the request that triggered it doesn't wait on embedding."""
    if not store.is_enabled():
        return

    async def run() -> None:
        try:
            await remember(submission_id, version)
        except Exception as e:
            logger.warning("Couldn't add the review of %s v%s to review memory: %s", submission_id, version, e)

    task = asyncio.create_task(run(), name=f"review-memory-{submission_id}-{version}")
    _tasks.add(task)
    task.add_done_callback(_tasks.discard)


async def remember_all_in_background() -> None:
    """Startup: catch up on anything decided while review memory was off or the server was down."""
    if not store.is_enabled():
        return
    try:
        counts = await remember_all()
        logger.info("Review memory up to date: %s", counts)
    except Exception as e:
        logger.warning("Couldn't bring review memory up to date: %s", e)


def query_text(inp: Dict[str, Any]) -> str:
    """The text a post is matched on: the same parts that are stored for past reviews."""
    transcript = inp.get("transcript")
    spoken = " ".join(s.get("text", "") for s in transcript) if isinstance(transcript, list) else ""
    text = "\n".join(p for p in (inp.get("caption") or "", spoken) if p.strip())[:TEXT_CHARS]
    return f"{inp.get('title') or ''}\n{text}".strip()


async def similar(inp: Dict[str, Any], submission_id: Optional[UUID] = None,
                  exclude_submission: bool = False) -> List[Dict[str, Any]]:
    """The past reviews most like this content, closest first, as {score, title, text, meta}.

    Earlier reviews of the same post are kept (what the reviewer asked to fix last time is the most useful
    precedent of all), except an approval of identical content, which would only confirm itself.
    exclude_submission leaves the post out entirely (the test set does, so a saved case can't see its own answer).
    """
    query = query_text(inp)
    if not store.is_enabled() or not query:
        return []
    rows = await store.search(query, kinds=[KIND], limit=MAX_PRECEDENTS + 6, min_score=MIN_SCORE)
    own = str(submission_id) if submission_id else None
    picked = []
    for r in rows:
        meta = r.get("meta") or {}
        if own and meta.get("submission_id") == own:
            if exclude_submission:
                continue
            identical = f"{r.get('title') or ''}\n{r.get('text') or ''}".strip() == query
            if identical and meta.get("decision") == "APPROVE":
                continue
        picked.append({"score": round(float(r.get("score", 0)), 3), "title": r.get("title"), "text": r.get("text"),
                       "meta": meta, "same_post": bool(own and meta.get("submission_id") == own)})
        if len(picked) >= MAX_PRECEDENTS:
            break
    return picked
