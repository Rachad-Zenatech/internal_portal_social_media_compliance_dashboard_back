"""Messaging Context: brand voice, facts, platform norms and audience profiles that the content analysis uses.

Entries are scoped like compliance rules (an empty list means every brand / audience / platform), so each piece of
content only gets the entries that apply to it.
"""

from typing import Any, Dict, List, Optional, Sequence
from uuid import UUID

from fastapi import HTTPException

from models.compliance.context_model import ContextEntry, ContextEntryIn
from models.compliance.submission_model import AUDIENCES, PLATFORMS
from postgresql_db.database import fetch_all, fetch_one
from services.ai.core import vector_store_service
from services.compliance.evaluation import reference_index_service
from services.shared.core.audit_service import log_audit_event

# The order sections appear in the analysis prompt and on the page.
KIND_ORDER = ["BRAND_VOICE", "FACT", "PLATFORM_NORM", "AUDIENCE_PROFILE"]
# Keep the prompt focused: at most this much context in total.
MAX_PROMPT_CHARS = 9000
# Facts are many (one per newsroom article): only the most relevant to the content come in, so there's room for the
# platform norms and audience profiles too.
MAX_FACTS = 12
MAX_FACT_CHARS = 4500

_SELECT = """
    SELECT m.*, u.full_name AS updated_by_name
    FROM messaging_context m LEFT JOIN users u ON u.id = m.updated_by
"""


def _out(r: Dict[str, Any]) -> ContextEntry:
    return ContextEntry(
        id=r["id"], kind=r["kind"], title=r["title"], body=r["body"], do_examples=r["do_examples"],
        dont_examples=r["dont_examples"], brands=list(r["brands"] or []), audiences=list(r["audiences"] or []),
        platforms=list(r["platforms"] or []), source=r["source"], as_of=r["as_of"], is_active=r["is_active"],
        is_starter=r["seed_key"] is not None, updated_at=r["updated_at"], updated_by_name=r.get("updated_by_name"),
        auto_source=r.get("auto_source"), source_url=r.get("source_url"),
    )


def _newest_first(as_of: Any) -> int:
    return -(as_of.toordinal() if as_of else 0)


async def list_entries(kind: Optional[str] = None) -> List[ContextEntry]:
    rows = await fetch_all(
        _SELECT + " WHERE m.deleted_at IS NULL AND ($1::text IS NULL OR m.kind = $1) ORDER BY m.kind, m.title",
        kind,
    )
    # Facts newest first (the newsroom adds one per article); the rest by title.
    return sorted(
        (_out(r) for r in rows),
        key=lambda e: (KIND_ORDER.index(e.kind), _newest_first(e.as_of) if e.kind == "FACT" else 0, e.title.lower()),
    )


async def _clean(data: ContextEntryIn) -> ContextEntryIn:
    brands = [b["code"] for b in await fetch_all("SELECT code FROM brands WHERE is_active")]
    for field, allowed in (("brands", brands), ("audiences", list(AUDIENCES)), ("platforms", PLATFORMS)):
        values = list(dict.fromkeys(v.strip() for v in getattr(data, field) if v and v.strip()))
        bad = [v for v in values if v not in allowed]
        if bad:
            raise HTTPException(status_code=422, detail=f"Unknown {field}: {', '.join(bad)}")
        setattr(data, field, values)
    data.title, data.body = data.title.strip(), data.body.strip()
    for field in ("do_examples", "dont_examples", "source"):
        setattr(data, field, (getattr(data, field) or "").strip() or None)
    return data


async def create_entry(data: ContextEntryIn, user_id: UUID) -> ContextEntry:
    data = await _clean(data)
    row = await fetch_one(
        """
        INSERT INTO messaging_context
            (kind, title, body, do_examples, dont_examples, brands, audiences, platforms, source, as_of, is_active,
             created_by, updated_by)
        VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $12) RETURNING id
        """,
        data.kind, data.title, data.body, data.do_examples, data.dont_examples, data.brands, data.audiences,
        data.platforms, data.source, data.as_of, data.is_active, user_id,
    )
    await log_audit_event("messaging_context", str(row["id"]), "CREATE_CONTEXT", user_id, data.model_dump(mode="json"))
    reference_index_service.sync_soon()
    return _out(await fetch_one(_SELECT + " WHERE m.id = $1", row["id"]))


async def update_entry(entry_id: UUID, data: ContextEntryIn, user_id: UUID) -> ContextEntry:
    data = await _clean(data)
    before = await fetch_one(_SELECT + " WHERE m.id = $1 AND m.deleted_at IS NULL", entry_id)
    if not before:
        raise HTTPException(status_code=404, detail="Context entry not found")
    await fetch_one(
        """
        UPDATE messaging_context
        SET kind = $2, title = $3, body = $4, do_examples = $5, dont_examples = $6, brands = $7, audiences = $8,
            platforms = $9, source = $10, as_of = $11, is_active = $12, updated_by = $13, updated_at = NOW()
        WHERE id = $1 RETURNING id
        """,
        entry_id, data.kind, data.title, data.body, data.do_examples, data.dont_examples, data.brands, data.audiences,
        data.platforms, data.source, data.as_of, data.is_active, user_id,
    )
    await log_audit_event("messaging_context", str(entry_id), "UPDATE_CONTEXT", user_id,
                          {"before": _out(before).model_dump(mode="json"), "after": data.model_dump(mode="json")})
    reference_index_service.sync_soon()
    return _out(await fetch_one(_SELECT + " WHERE m.id = $1", entry_id))


async def delete_entry(entry_id: UUID, user_id: UUID) -> None:
    """Soft delete, so a removed starter entry isn't seeded again and the audit trail still resolves."""
    before = await fetch_one(_SELECT + " WHERE m.id = $1 AND m.deleted_at IS NULL", entry_id)
    if not before:
        raise HTTPException(status_code=404, detail="Context entry not found")
    await fetch_one(
        "UPDATE messaging_context SET deleted_at = NOW(), updated_by = $2, updated_at = NOW() WHERE id = $1 RETURNING id",
        entry_id, user_id,
    )
    await log_audit_event("messaging_context", str(entry_id), "DELETE_CONTEXT", user_id, _out(before).model_dump(mode="json"))
    reference_index_service.sync_soon()


# ---------------------------------------------------------------------------
# For the analysis
# ---------------------------------------------------------------------------

THOUGHT_LEADERSHIP_BRAND = "CEO_THOUGHT_LEADERSHIP"


def with_thought_leadership(brand_codes: Sequence[str], thought_leadership: bool) -> List[str]:
    """The brands whose context applies: a post on a thought leadership account (Shaun's channel) also gets the
    thought leadership brand guide, even when it was filed under another brand."""
    brands = list(brand_codes)
    if thought_leadership and THOUGHT_LEADERSHIP_BRAND not in brands:
        brands.append(THOUGHT_LEADERSHIP_BRAND)
    return brands


def _applies(values: List[str], content: List[str]) -> bool:
    return not values or bool(set(values) & set(content))


async def for_content(brand_codes: Sequence[str], audiences: Sequence[str], platforms: List[str],
                      text: str = "") -> List[Dict[str, Any]]:
    """The active entries that apply to this content, in prompt order, trimmed to a sensible total size. Platform
    norms and audience profiles only come in when they match; brand voice and facts can also apply to everything.
    Facts: the ones closest in meaning to the content's text first (when the vector store is on), then the newest."""
    rows = await fetch_all(
        "SELECT * FROM messaging_context WHERE deleted_at IS NULL AND is_active ORDER BY kind, title"
    )
    picked: List[Dict[str, Any]] = []
    for r in rows:
        if not _applies(list(r["brands"] or []), list(brand_codes)):
            continue
        if not _applies(list(r["audiences"] or []), list(audiences)):
            continue
        if not _applies(list(r["platforms"] or []), platforms):
            continue
        # A platform norm or audience profile without a scope would apply everywhere; only matching ones help.
        if r["kind"] == "PLATFORM_NORM" and not r["platforms"]:
            continue
        if r["kind"] == "AUDIENCE_PROFILE" and not r["audiences"]:
            continue
        picked.append({
            "id": str(r["id"]),
            "kind": r["kind"],
            "title": r["title"],
            "body": r["body"],
            "do": r["do_examples"],
            "dont": r["dont_examples"],
            "source": r["source"],
            "as_of": r["as_of"].isoformat() if r["as_of"] else None,
            # Editing an entry makes earlier analyses that used it out of date.
            "updated_at": r["updated_at"].isoformat(),
            "_newest": _newest_first(r["as_of"]),
        })
    relevant: Dict[str, int] = {}
    if text.strip() and sum(1 for e in picked if e["kind"] == "FACT") > MAX_FACTS:
        hits = await vector_store_service.search(
            text[:4000], kinds=["FACT"], brand=list(brand_codes) or None, audience=list(audiences) or None,
            platforms=platforms or None, limit=MAX_FACTS,
        )
        relevant = {h["source_id"]: n for n, h in enumerate(hits)}
    picked.sort(key=lambda e: (KIND_ORDER.index(e["kind"]), relevant.get(e["id"], len(relevant)), e["_newest"]))
    kept, total, facts, fact_chars = [], 0, 0, 0
    for e in picked:
        size = len(e["body"]) + len(e["do"] or "") + len(e["dont"] or "")
        if total + size > MAX_PROMPT_CHARS:
            continue
        if e["kind"] == "FACT":
            if facts >= MAX_FACTS or fact_chars + size > MAX_FACT_CHARS:
                continue
            facts, fact_chars = facts + 1, fact_chars + size
        e.pop("_newest", None)
        kept.append(e)
        total += size
    return kept
