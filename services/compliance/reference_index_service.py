"""Reference index: keeps the Messaging Context (brand voice, facts, platform norms, audience profiles) and the
compliance rulebook in the vector store, so the assistant can retrieve the passages that answer a question
(search_reference_material_tool) instead of being handed everything.

The whole set is re-synced after any change and once on start. That is cheap: vector_store_service.upsert only
embeds text that changed, and removed or switched-off entries are deleted from the store. Off (does nothing) while
the vector store isn't configured.
"""

import asyncio
import logging
from typing import Any, Dict, List, Optional

from postgresql_db.database import fetch_all
from services.ai import vector_store_service as store

logger = logging.getLogger(__name__)

RULE_KIND = "COMPLIANCE_RULE"
CONTEXT_KINDS = ["BRAND_VOICE", "FACT", "PLATFORM_NORM", "AUDIENCE_PROFILE"]
KINDS = CONTEXT_KINDS + [RULE_KIND]

_task: Optional[asyncio.Task] = None
_again = False


def _lines(*parts: Any) -> str:
    return "\n".join(str(p).strip() for p in parts if p and str(p).strip())


def _context_doc(r: Dict[str, Any]) -> store.VectorDoc:
    return store.VectorDoc(
        kind=r["kind"], source_id=str(r["id"]), title=r["title"],
        text=_lines(r["body"], r["do_examples"] and f"Do: {r['do_examples']}",
                    r["dont_examples"] and f"Don't: {r['dont_examples']}", r["source"] and f"Source: {r['source']}"),
        brands=list(r["brands"] or []), audiences=list(r["audiences"] or []), platforms=list(r["platforms"] or []),
        meta={"as_of": r["as_of"].isoformat() if r["as_of"] else None},
    )


def _rule_doc(r: Dict[str, Any]) -> store.VectorDoc:
    return store.VectorDoc(
        kind=RULE_KIND, source_id=str(r["id"]), title=f"{r['rule_number']}: {r['title']}",
        text=_lines(r["summary"],
                    r["prohibited_terms"] and f"Prohibited terms: {', '.join(r['prohibited_terms'])}",
                    r["required_disclaimers"] and f"Required disclaimers: {'; '.join(r['required_disclaimers'])}",
                    r["allowed_examples"] and f"Allowed: {r['allowed_examples']}",
                    r["prohibited_examples"] and f"Not allowed: {r['prohibited_examples']}",
                    r["remediation_guidance"] and f"How to fix: {r['remediation_guidance']}"),
        brands=list(r["brands"] or []), audiences=list(r["audiences"] or []), platforms=list(r["platforms"] or []),
        meta={"rule_number": r["rule_number"], "category": r["category"], "severity": r["severity"],
              "traffic_light": r["traffic_light"], "rule_sets": list(r["rule_sets"] or [])},
    )


async def sync_all() -> Dict[str, int]:
    if not store.is_enabled():
        return {"embedded": 0, "unchanged": 0, "removed": 0}
    contexts = await fetch_all("SELECT * FROM messaging_context")
    rules = await fetch_all("SELECT * FROM compliance_rules")
    live = [_context_doc(r) for r in contexts if r["deleted_at"] is None and r["is_active"]]
    live += [_rule_doc(r) for r in rules if r["deleted_at"] is None and r["is_active"]]
    gone = [f"{r['kind']}:{r['id']}" for r in contexts if r["deleted_at"] is not None or not r["is_active"]]
    gone += [f"{RULE_KIND}:{r['id']}" for r in rules if r["deleted_at"] is not None or not r["is_active"]]
    result = await store.upsert(live)
    result["removed"] = await store.delete(gone)
    return result


def sync_soon() -> None:
    """Re-sync in the background after a change (a change made while a sync is running triggers one more)."""
    global _task, _again
    if _task and not _task.done():
        _again = True
        return

    async def run() -> None:
        global _again
        while True:
            _again = False
            try:
                result = await sync_all()
                if result["embedded"] or result["removed"]:
                    logger.info("Reference index updated: %s", result, extra={"event": "reference_index_synced"})
            except Exception as e:
                logger.warning("Couldn't update the reference index (the assistant may quote older material): %s", e)
            if not _again:
                return

    _task = asyncio.create_task(run(), name="reference-index-sync")


async def search(query: str, kinds: Optional[List[str]] = None, brand: Optional[str] = None,
                 limit: int = 5) -> List[Dict[str, Any]]:
    wanted = [k for k in (kinds or KINDS) if k in KINDS] or KINDS
    rows = await store.search(query, kinds=wanted, brand=brand, limit=limit, min_score=0.6)
    return [
        {"kind": r.get("kind"), "title": r.get("title"), "text": r.get("text"), "score": round(r.get("score", 0), 3),
         **{k: v for k, v in (r.get("meta") or {}).items() if v}}
        for r in rows
    ]
