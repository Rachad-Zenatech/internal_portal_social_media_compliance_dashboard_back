"""What reviewers say about an assessment, kept for learning (the screen's own state lives in Postgres, see
conversation_feedback; these writes mirror it, best-effort, into MongoDB).

Two collections, because they teach different things and must not be mixed:

    mood_reviews            the reviewer's DECISION on the assessment: POST, WAIT or HOLD, and why. This is the label for
                            the recommendation model. One per reviewer per assessment, changeable (earlier answers are
                            kept in `previous`), withdrawable (`retracted_at`, never deleted).
    mood_match_corrections  corrections to the matching or the reading, per item:
                              UNRELATED        "this conversation isn't about the post"   -> improves matching
                              WRONG_SENTIMENT  "this is not negative/positive/neutral"    -> improves sentiment analysis

Reviewers can disagree. Before an assessment is used for training its decision is resolved (resolve_decision): one
reviewer, or several agreeing, is accepted; a conflict is excluded until a lead reviewer's resolution settles it.
"""

import logging
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional
from uuid import UUID

from services.ai.core import mood_store

logger = logging.getLogger(__name__)

DECISION_LABELS = ("POST", "WAIT", "HOLD")
SENTIMENTS = ("POSITIVE", "NEUTRAL", "NEGATIVE")
UNRELATED, WRONG_SENTIMENT = "UNRELATED", "WRONG_SENTIMENT"


@dataclass
class Resolution:
    status: str                       # ACCEPTED | CONFLICT | UNREVIEWED
    label: Optional[str] = None       # the accepted decision when ACCEPTED
    source: Optional[str] = None      # SINGLE_REVIEWER | UNANIMOUS | RESOLUTION
    review_ids: List[str] = field(default_factory=list)  # the review records the label rests on
    labels: List[str] = field(default_factory=list)      # what the reviewers said (to show a conflict)


def resolve_decision(reviews: List[Dict[str, Any]]) -> Resolution:
    """The accepted decision for an assessment from its active (not withdrawn) reviews, or why there isn't one."""
    active = [r for r in reviews if not r.get("retracted_at")]
    if not active:
        return Resolution("UNREVIEWED")
    ids = [r["_id"] for r in active]
    resolutions = sorted((r for r in active if r.get("is_resolution")), key=lambda r: r["reviewed_at"])
    if resolutions:  # a lead reviewer's resolution settles a disagreement
        last = resolutions[-1]
        return Resolution("ACCEPTED", last["reviewer_label"], "RESOLUTION", [last["_id"]], sorted({r["reviewer_label"] for r in active}))
    labels = sorted({r["reviewer_label"] for r in active})
    if len(labels) > 1:
        return Resolution("CONFLICT", None, None, ids, labels)
    return Resolution("ACCEPTED", labels[0], "SINGLE_REVIEWER" if len(active) == 1 else "UNANIMOUS", ids, labels)


def _check(value: str, allowed) -> str:
    value = (value or "").strip().upper()
    if value not in allowed:
        raise ValueError(f"'{value}' is not one of {', '.join(allowed)}")
    return value


# --- decisions ---------------------------------------------------------------------------------------------------

async def record_decision(assessment_id: UUID, submission_id: UUID, reviewer_id: UUID, label: str, reason_code: str,
                          comment: Optional[str], resolution: bool = False) -> Dict[str, Any]:
    """Save (or change) a reviewer's decision. A changed answer keeps the earlier one in `previous`."""
    label = _check(label, DECISION_LABELS)
    doc_id = f"{assessment_id}:{reviewer_id}:decision"
    existing = await mood_store.get(mood_store.REVIEWS, doc_id)
    previous = list(existing.get("previous", [])) if existing else []
    if existing and not existing.get("retracted_at"):
        previous.append({k: existing.get(k) for k in ("reviewer_label", "reason_code", "comment", "reviewed_at", "is_resolution")})
    doc = {
        "_id": doc_id, "assessment_id": str(assessment_id), "submission_id": str(submission_id),
        "reviewer_id": str(reviewer_id), "reviewer_label": label, "reason_code": reason_code,
        "comment": (comment or "").strip() or None, "is_resolution": bool(resolution),
        "reviewed_at": mood_store.now(), "retracted_at": None, "previous": previous,
    }
    await mood_store.upsert(mood_store.REVIEWS, doc)
    return doc


async def retract_decision(assessment_id: UUID, reviewer_id: UUID) -> None:
    await mood_store.update(mood_store.REVIEWS, f"{assessment_id}:{reviewer_id}:decision", {"retracted_at": mood_store.now()})


async def decisions_for(assessment_id: str) -> List[Dict[str, Any]]:
    """Every review record of an assessment, withdrawn ones included (resolve_decision ignores those)."""
    docs = await mood_store.find(mood_store.REVIEWS, {"assessment_id": str(assessment_id)})
    for d in docs:
        d["reviewed_at"] = mood_store.aware(d.get("reviewed_at"))
    return docs


# --- corrections to matching and sentiment ---------------------------------------------------------------------

async def record_match_correction(assessment_id: UUID, submission_id: UUID, reviewer_id: UUID, kind: str, item_id: UUID,
                                  reason_code: Optional[str] = None, correct_sentiment: Optional[str] = None,
                                  comment: Optional[str] = None) -> Dict[str, Any]:
    """An item-level correction: UNRELATED (with the reason) or WRONG_SENTIMENT (with what it should have read)."""
    kind = _check(kind, (UNRELATED, WRONG_SENTIMENT))
    if kind == WRONG_SENTIMENT:
        correct_sentiment = _check(correct_sentiment or "", SENTIMENTS)
    doc = {
        "_id": f"{assessment_id}:{reviewer_id}:{kind}:{item_id}", "assessment_id": str(assessment_id),
        "submission_id": str(submission_id), "reviewer_id": str(reviewer_id), "kind": kind, "item_id": str(item_id),
        "reason_code": reason_code, "correct_sentiment": correct_sentiment, "comment": (comment or "").strip() or None,
        "reviewed_at": mood_store.now(), "retracted_at": None,
    }
    await mood_store.upsert(mood_store.MATCH_CORRECTIONS, doc)
    return doc


async def retract_match_corrections(submission_id: UUID, item_id: UUID, kind: str,
                                    reviewer_id: Optional[UUID] = None) -> int:
    """Withdraw corrections of one kind on an item for a post (everyone's, or just one reviewer's)."""
    col = mood_store.collection(mood_store.MATCH_CORRECTIONS)
    if col is None:
        return 0
    flt: Dict[str, Any] = {"submission_id": str(submission_id), "item_id": str(item_id), "kind": kind, "retracted_at": None}
    if reviewer_id is not None:
        flt["reviewer_id"] = str(reviewer_id)
    res = await col.update_many(flt, {"$set": {"retracted_at": mood_store.now()}})
    return res.modified_count


async def mirror(coro) -> None:
    """Run a Mongo write that mirrors something already saved: a failure is logged, never raised, so a reviewer's action
    isn't lost because the learning store was unreachable."""
    if not mood_store.is_enabled():
        coro.close()
        return
    try:
        await coro
    except Exception as e:
        logger.warning("Couldn't copy a reviewer action to the learning store: %s", e)
