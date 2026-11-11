"""Saving an assessment for learning: when the system evaluates a draft it writes one record to `mood_assessments`.

The record says exactly what was assessed (the draft's content and version), what it was assessed against (the evidence
items with their content versions, and a snapshot of each so history doesn't depend on the live feed), what was measured
(the features, built by mood_features), and what the system said and why. It is insert-only: reassessing a draft makes a
new record, so every recommendation a reviewer ever saw can be reproduced and every one can become a training example.

Saving is best-effort: a failure here is logged and never stops the assessment the reviewer is waiting for.
"""

import logging
from datetime import datetime
from decimal import Decimal
from typing import Any, Dict, List, Optional
from uuid import UUID, uuid4

from services.ai.core import mood_store

logger = logging.getLogger(__name__)

MAX_FIELD_CHARS = 50_000  # a draft's text is kept in full for reproducibility, but not without bound


def _plain(value: Any) -> Any:
    """A value Mongo can store: UUIDs as text, Decimals as numbers, containers walked."""
    if isinstance(value, UUID):
        return str(value)
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, dict):
        return {k: _plain(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_plain(v) for v in value]
    return value


def draft_context_of(row: Dict[str, Any]) -> Dict[str, Any]:
    """The draft as it was when assessed: who it is for and what it says."""
    text = lambda k: (row.get(k) or "")[:MAX_FIELD_CHARS]
    return {
        "brand_code": row.get("brand_code"), "brand_name": row.get("brand_name"), "audience": row.get("audience"),
        "platforms": sorted(row.get("platforms") or []), "title": text("title"), "caption_text": text("caption_text"),
        "script_text": text("script_text"), "transcript_text": text("transcript_text"),
    }


def evidence_dict(item: Dict[str, Any], verdict: Optional[str] = None) -> Dict[str, Any]:
    """An evidence item as the features and the saved snapshot need it."""
    return {
        "item_id": str(item["id"]), "item_version": item.get("version"), "content_hash": item.get("content_hash"),
        "story_key": item.get("story_key"), "text": item.get("text"), "url": item.get("url"),
        "author": item.get("author"), "network": item.get("network"), "topic": item.get("topic_name"),
        "category": item.get("category"), "published_at": item.get("published_at"),
        "first_seen_at": item.get("first_seen_at"), "sentiment": item.get("sentiment"),
        "compound": float(item["compound"]) if item.get("compound") is not None else None,
        "crisis_flags": list(item.get("crisis_flags") or []),
    }


def build_assessment_record(*, assessment_id: Optional[UUID], submission_id: UUID, draft_version: int,
                            row: Dict[str, Any], evidence_rows: List[Any], mood: Any, features: Dict[str, Any],
                            feature_schema_version: str, policy_version: str, embedding_version: str,
                            evidence_version: str, state: Dict[str, Any], expires_at: datetime,
                            decision: Optional[Dict[str, Any]] = None, is_mock: bool = False) -> Dict[str, Any]:
    """The record for one assessment. `evidence_rows` are every candidate the assessment looked at (relevant or not),
    so the reviewer's view and the verdict on each can be reproduced."""
    aid = str(assessment_id or uuid4())
    evidence = []
    for r in evidence_rows:
        snap = evidence_dict(r.item)
        snap.update(similarity_score=round(float(r.similarity), 4), relevance_status=r.status,
                    relevance_reason=r.reason, supporting_text=r.supporting, counted=bool(r.counted),
                    displayed=bool(r.displayed))
        evidence.append(snap)
    return _plain({
        "_id": aid,
        "assessment_id": aid,
        "submission_id": str(submission_id),
        "draft_version": draft_version,
        "draft_context": draft_context_of(row),
        # What it was assessed against: ids and content versions, plus a snapshot of each item.
        "evidence_refs": [{"item_id": e["item_id"], "item_version": e["item_version"], "content_hash": e["content_hash"],
                           "relevance_status": e["relevance_status"], "counted": e["counted"]} for e in evidence],
        "evidence": evidence,
        "features": features,
        "feature_schema_version": feature_schema_version,
        "policy_version": policy_version,
        "embedding_version": embedding_version,
        "evidence_version": evidence_version,
        "evidence_state": {"collection_status": state.get("collection_status"), "collected_at": state.get("collected_at"),
                           "indexing_status": state.get("indexing_status")},
        "status": mood.status,
        "system_recommendation": mood.recommendation,
        "reason_codes": list(mood.reason_codes),
        "counts": {k: getattr(mood, k) for k in ("retrieved_count", "verified_relevant_count", "distinct_story_count",
                                                  "uncertain_count", "unrelated_count", "outdated_count", "retrieval_capped")},
        "decision": decision or {"source": "RULES"},
        "assessed_at": mood.assessed_at or mood_store.now(),
        "expires_at": expires_at,
        "is_mock": is_mock,
    })


async def record_assessment(record: Dict[str, Any]) -> bool:
    """Write the record; True if it was saved. Never raises."""
    if not mood_store.is_enabled():
        return False
    try:
        return await mood_store.insert_once(mood_store.ASSESSMENTS, record)
    except Exception as e:
        logger.warning("Couldn't save the assessment record for learning: %s", e)
        return False
