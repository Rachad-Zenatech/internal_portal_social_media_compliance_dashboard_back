"""What the XGBoost decision model was trained on (and live reviewed assessments queued for training),
with post details, reviewer comment notes, and scraped evidence for the Training Data & Quality page."""

import json
import logging
from collections import Counter, defaultdict
from datetime import timezone
from typing import Any, Dict, List, Optional
from uuid import UUID

from postgresql_db.database import fetch_all, fetch_one, get_pool
from services.ai.core import mood_store
from services.compliance.analytics import mood_auto_train, mood_dataset, mood_model, mood_reviews, mood_training

logger = logging.getLogger(__name__)

MAX_EXAMPLES = 200
MAX_EVIDENCE_PREVIEW = 8


def _post_decision_to_mood(decision: Optional[str], system_rec: Optional[str]) -> Optional[str]:
    if decision == "APPROVE":
        return "POST"
    if decision == "REQUEST_CHANGES":
        return "HOLD" if system_rec == "HOLD" else "WAIT"
    return None


async def sync_post_decision_to_mood(submission_id: UUID, version: int) -> None:
    """When a reviewer decides on a post (or leaves review comments and approves/requests changes),
    mirror that decision onto the latest public-mood assessment of that submission if the reviewer
    hasn't already explicitly labelled that assessment."""
    job = await fetch_one(
        """
        SELECT j.decision, j.decision_notes, j.decided_by,
               COALESCE((
                   SELECT string_agg(c.body, ' | ' ORDER BY c.created_at)
                   FROM content_review_comments c
                   WHERE c.submission_id = j.submission_id AND c.version_number = j.version_number
                     AND c.deleted_at IS NULL AND c.from_reviewer IS NOT FALSE
               ), '') AS reviewer_comments
        FROM content_review_jobs j
        WHERE j.submission_id = $1 AND j.version_number = $2 AND j.decision IS NOT NULL AND j.decided_by IS NOT NULL
        ORDER BY j.decided_at DESC NULLS LAST LIMIT 1
        """,
        submission_id, version,
    )
    if not job:
        return
    assessment = await fetch_one(
        """
        SELECT id, result->>'recommendation' AS rec
        FROM public_mood_assessments
        WHERE submission_id = $1
        ORDER BY (version_number = $2) DESC, assessed_at DESC
        LIMIT 1
        """,
        submission_id, version,
    )
    if not assessment:
        return
    rec = _post_decision_to_mood(job["decision"], assessment["rec"])
    if not rec:
        return
    note_parts = [p.strip() for p in (job["decision_notes"] or "", job["reviewer_comments"] or "") if p and p.strip()]
    note = " — ".join(note_parts)[:500] or None
    reason_code = "AGREE" if rec == assessment["rec"] else ("TOO_CAUTIOUS" if rec == "POST" else "TOO_RISKY")
    existing = await fetch_one(
        """
        SELECT 1 FROM public_mood_assessment_feedback
        WHERE assessment_id = $1 AND user_id = $2 AND kind = 'RECOMMENDATION_CORRECTION'
        """,
        assessment["id"], job["decided_by"],
    )
    if existing:
        return
    await get_pool().execute(
        """
        INSERT INTO public_mood_assessment_feedback (assessment_id, kind, recommendation, reason_code, note, user_id)
        VALUES ($1, 'RECOMMENDATION_CORRECTION', $2, $3, $4, $5)
        ON CONFLICT (assessment_id, user_id) WHERE kind = 'RECOMMENDATION_CORRECTION'
        DO NOTHING
        """,
        assessment["id"], rec, reason_code, note, job["decided_by"],
    )
    await mood_reviews.mirror(
        mood_reviews.record_decision(assessment["id"], submission_id, job["decided_by"], rec, reason_code, note)
    )
    mood_auto_train.schedule("post_review")


async def _backfill_from_postgres() -> None:
    """Ensure any reviewed assessments or decided posts in Postgres are mirrored into mood_store so they show up."""
    if not mood_store.is_enabled():
        return
    rows = await fetch_all(
        """
        SELECT DISTINCT ON (s.id) s.id AS submission_id, s.current_version
        FROM content_submissions s
        JOIN content_review_jobs j ON j.submission_id = s.id
        WHERE j.decision IS NOT NULL AND s.deleted_at IS NULL
        ORDER BY s.id, j.decided_at DESC NULLS LAST
        LIMIT 50
        """
    )
    for r in rows:
        try:
            await sync_post_decision_to_mood(r["submission_id"], r["current_version"])
        except Exception:
            pass


async def _current_version() -> Optional[str]:
    """The dataset the registered model learned from, else the newest finished dataset."""
    status = await mood_model.model_status()
    version = (status.get("training_summary") or {}).get("dataset_version")
    if version:
        return str(version)
    datasets = await mood_store.find(mood_store.DATASETS, {"status": "READY"}, sort=[("created_at", -1)], limit=1)
    return str(datasets[0]["_id"]) if datasets else None


async def _enrich_submissions(rows: List[Dict[str, Any]], assessments_by_id: Dict[str, Dict[str, Any]]) -> None:
    sub_ids: List[UUID] = []
    for r in rows:
        sid = r.get("submission_id")
        if sid:
            try:
                sub_ids.append(UUID(str(sid)))
            except (ValueError, TypeError):
                pass
    sub_map: Dict[str, Dict[str, Any]] = {}
    if sub_ids:
        db_rows = await fetch_all(
            """
            SELECT s.id::text AS id, s.reference_code, s.title, s.content_type, s.brand_code, s.status,
                   COALESCE((
                       SELECT json_agg(c.body ORDER BY c.created_at)
                       FROM content_review_comments c
                       WHERE c.submission_id = s.id AND c.deleted_at IS NULL AND c.from_reviewer IS NOT FALSE
                   ), '[]'::json) AS reviewer_comments,
                   (SELECT j.decision_notes FROM content_review_jobs j
                    WHERE j.submission_id = s.id AND j.decision_notes IS NOT NULL
                    ORDER BY j.decided_at DESC NULLS LAST LIMIT 1) AS decision_notes,
                   (SELECT count(*) FROM content_submission_files f
                    WHERE f.submission_id = s.id AND f.deleted_at IS NULL) AS file_count
            FROM content_submissions s
            WHERE s.id = ANY($1::uuid[])
            """,
            sub_ids,
        )
        for d in db_rows:
            comments = json.loads(d["reviewer_comments"]) if isinstance(d["reviewer_comments"], str) else (d["reviewer_comments"] or [])
            sub_map[d["id"]] = {
                "reference_code": d["reference_code"],
                "title": d["title"],
                "content_type": d["content_type"],
                "brand_code": d["brand_code"],
                "status": d["status"],
                "reviewer_comments": [c for c in comments if c][:6],
                "decision_notes": d["decision_notes"],
                "file_count": int(d["file_count"] or 0),
            }
    for r in rows:
        a = assessments_by_id.get(str(r.get("assessment_id") or "")) or {}
        dc = a.get("draft_context") or {}
        sub = sub_map.get(str(r.get("submission_id") or "")) or {}
        r["post_title"] = sub.get("title") or dc.get("title") or "Untitled draft"
        r["reference_code"] = sub.get("reference_code")
        r["content_type"] = sub.get("content_type") or "SOCIAL_POST"
        r["brand_code"] = sub.get("brand_code") or dc.get("brand_code")
        r["post_status"] = sub.get("status")
        r["file_count"] = sub.get("file_count", 0)
        r["reviewer_comments"] = sub.get("reviewer_comments") or []
        r["decision_notes"] = sub.get("decision_notes")
        ev = a.get("evidence") or []
        r["evidence_items"] = [
            {
                "text": e.get("text"),
                "url": e.get("url"),
                "author": e.get("author"),
                "network": e.get("network"),
                "sentiment": e.get("sentiment"),
                "relevance_status": e.get("relevance_status"),
            }
            for e in ev if e.get("counted") or e.get("displayed")
        ][:MAX_EVIDENCE_PREVIEW]


async def training_examples(version: Optional[str] = None) -> Dict[str, Any]:
    """Return the fixed dataset examples (or live candidate examples if no fixed dataset exists yet),
    plus readiness progress toward the per-class XGBoost training threshold."""
    min_needed = {c: sum(mood_training.MIN_PER_CLASS.values()) for c in mood_training.CLASSES}
    empty_readiness = {
        "ready": False,
        "min_per_class": mood_training.MIN_PER_CLASS,
        "total_needed_per_class": sum(mood_training.MIN_PER_CLASS.values()),
        "class_totals": {c: 0 for c in mood_training.CLASSES},
        "class_counts": {s: {c: 0 for c in mood_training.CLASSES} for s in mood_dataset.SPLITS},
        "shortfalls": [f"{c}: 0 of {min_needed[c]} needed" for c in mood_training.CLASSES],
        "unreviewed_assessments": 0,
    }
    if not mood_store.is_enabled():
        return {"dataset_version": None, "is_live_preview": True, "total": 0, "splits": {}, "examples": [], "readiness": empty_readiness}

    await _backfill_from_postgres()
    version = version or await _current_version()
    is_live_preview = False
    examples: List[Dict[str, Any]] = []
    excluded: Counter = Counter()

    if version:
        try:
            _, examples = await mood_dataset.load_dataset(version)
        except mood_dataset.DatasetError:
            examples = []

    if not examples:
        is_live_preview = True
        try:
            examples, excluded = await mood_dataset.select_examples("live_preview")
        except mood_dataset.DatasetError:
            # Even if select_examples raises (e.g. 0 reviewed or too few groups), inspect raw assessments + reviews
            assessments = await mood_store.find(mood_store.ASSESSMENTS, {}, sort=[("assessed_at", 1)])
            reviews_by_aid: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
            if assessments:
                ids = [a["_id"] for a in assessments]
                for rev in await mood_store.find(mood_store.REVIEWS, {"assessment_id": {"$in": ids}}):
                    rev["reviewed_at"] = mood_store.aware(rev.get("reviewed_at"))
                    reviews_by_aid[rev["assessment_id"]].append(rev)
            for a in assessments:
                if a.get("is_mock") or not a.get("features"):
                    continue
                res = mood_reviews.resolve_decision(reviews_by_aid.get(a["_id"], []))
                if res.status != "ACCEPTED":
                    excluded["not_reviewed"] += 1
                    continue
                code, reason = mood_dataset._decisive_reason(reviews_by_aid.get(a["_id"], []), res)
                examples.append({
                    "dataset_version": "live_preview",
                    "assessment_id": a["_id"],
                    "assessed_at": mood_store.aware(a.get("assessed_at")),
                    "feature_schema_version": a.get("feature_schema_version", "v1"),
                    "features": a.get("features") or {},
                    "system_recommendation": a.get("system_recommendation"),
                    "reviewer_label": res.label,
                    "review_reason": reason,
                    "review_reason_code": code,
                    "submission_id": a.get("submission_id"),
                    "review_ids": res.review_ids,
                    "label_source": res.source,
                    "group_id": "g1",
                    "split": "train",
                })

    splits: Dict[str, int] = {}
    class_totals = {c: 0 for c in mood_training.CLASSES}
    for e in examples:
        sp = e.get("split") or "train"
        splits[sp] = splits.get(sp, 0) + 1
        lbl = e.get("reviewer_label")
        if lbl in class_totals:
            class_totals[lbl] += 1

    ready_info = mood_training.readiness(examples) if examples else empty_readiness
    readiness = {
        "ready": ready_info["ready"],
        "min_per_class": mood_training.MIN_PER_CLASS,
        "total_needed_per_class": sum(mood_training.MIN_PER_CLASS.values()),
        "class_totals": class_totals,
        "class_counts": ready_info["class_counts"],
        "shortfalls": ready_info["shortfalls"],
        "unreviewed_assessments": int(excluded.get("not_reviewed", 0)),
    }

    recent = examples[-MAX_EXAMPLES:]
    recent.reverse()  # newest first for the table
    rows = [mood_dataset.training_row(e) for e in recent]

    aids = [r["assessment_id"] for r in rows if r.get("assessment_id")]
    assessments_by_id: Dict[str, Dict[str, Any]] = {}
    if aids:
        for a in await mood_store.find(mood_store.ASSESSMENTS, {"_id": {"$in": aids}}):
            assessments_by_id[str(a["_id"])] = a

    await _enrich_submissions(rows, assessments_by_id)
    return {
        "dataset_version": None if is_live_preview else version,
        "is_live_preview": is_live_preview,
        "total": len(examples),
        "splits": splits,
        "examples": rows,
        "readiness": readiness,
    }
