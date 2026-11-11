"""Reviewer input on a conversation assessment, kept against the exact assessment the reviewer was looking at.

Two things a reviewer can say, each with a reason so it can be explained (and later learned from):
- "This item isn't about the post" (MATCH_UNRELATED): the item is left out of this draft's later assessments, and shows
  under `excluded_by_reviewers` until someone puts it back.
- "This recommendation is wrong" (RECOMMENDATION_CORRECTION): shown beside the assessment. It doesn't change what the
  assessment computed; public mood stays advisory.
"""

from typing import Any, Dict, List, Optional, Set
from uuid import UUID

from fastapi import HTTPException

from models.compliance.public_mood_model import ExcludedMatch, ReviewerCorrection
from postgresql_db.database import fetch_all, fetch_one, get_pool
from services.compliance.analytics import mood_auto_train, mood_reviews

UNRELATED_REASONS: Dict[str, str] = {
    "DIFFERENT_COMPANY": "About a different company",
    "DIFFERENT_PRODUCT_OR_TOPIC": "About a different product or topic",
    "DIFFERENT_EVENT_OR_PLACE": "About a different event or place",
    "OUTDATED": "Too old to matter",
    "DUPLICATE_STORY": "The same story is already counted",
    "NOT_ABOUT_THIS_POST": "Not about what this post says",
    "OTHER": "Other (explain in the note)",
}
CORRECTION_REASONS: Dict[str, str] = {
    "AGREE": "Agree with the recommendation",
    "TOO_CAUTIOUS": "Too cautious: it is fine to post",
    "TOO_RISKY": "Too relaxed: it needs more care",
    "WRONG_SENTIMENT": "The sentiment of the evidence is misread",
    "BAD_EVIDENCE": "The matched items aren't about this post",
    "TOO_FEW_SOURCES": "Too few sources to say",
    "STALE_EVIDENCE": "The evidence is out of date",
    "OTHER": "Other (explain in the note)",
}


def reasons() -> Dict[str, Dict[str, str]]:
    return {"unrelated": UNRELATED_REASONS, "correction": CORRECTION_REASONS}


def _check_reason(code: str, allowed: Dict[str, str]) -> str:
    code = (code or "").strip().upper()
    if code not in allowed:
        raise HTTPException(status_code=422, detail=f"Unknown reason '{code}'. Choose one of: {', '.join(allowed)}")
    return code


async def assessment_of(submission_id: UUID, assessment_id: UUID) -> Dict[str, Any]:
    """The assessment, only if it belongs to this submission."""
    row = await fetch_one(
        "SELECT id, submission_id FROM public_mood_assessments WHERE id = $1 AND submission_id = $2",
        assessment_id, submission_id,
    )
    if not row:
        raise HTTPException(status_code=404, detail="Assessment not found for this post")
    return dict(row)


async def mark_unrelated(submission_id: UUID, assessment_id: UUID, item_id: UUID, user_id: UUID, reason_code: str,
                         note: Optional[str]) -> None:
    code = _check_reason(reason_code, UNRELATED_REASONS)
    await assessment_of(submission_id, assessment_id)
    seen = await fetch_one(
        "SELECT 1 AS ok FROM public_mood_assessment_items WHERE assessment_id = $1 AND item_id = $2",
        assessment_id, item_id,
    )
    if not seen:
        raise HTTPException(status_code=404, detail="That item wasn't part of this assessment")
    await get_pool().execute(
        """
        INSERT INTO public_mood_assessment_feedback (assessment_id, kind, item_id, reason_code, note, user_id)
        VALUES ($1, 'MATCH_UNRELATED', $2, $3, $4, $5)
        ON CONFLICT (assessment_id, user_id, item_id) WHERE kind = 'MATCH_UNRELATED'
        DO UPDATE SET reason_code = EXCLUDED.reason_code, note = EXCLUDED.note, created_at = NOW()
        """,
        assessment_id, item_id, code, (note or "").strip() or None, user_id,
    )
    await mood_reviews.mirror(mood_reviews.record_match_correction(
        assessment_id, submission_id, user_id, mood_reviews.UNRELATED, item_id, reason_code=code, comment=note))


async def restore_item(submission_id: UUID, item_id: UUID) -> None:
    """Put an item back: removes every reviewer's 'unrelated' mark on it for this post."""
    await get_pool().execute(
        """
        DELETE FROM public_mood_assessment_feedback f USING public_mood_assessments a
        WHERE f.assessment_id = a.id AND a.submission_id = $1 AND f.kind = 'MATCH_UNRELATED' AND f.item_id = $2
        """,
        submission_id, item_id,
    )
    await mood_reviews.mirror(mood_reviews.retract_match_corrections(submission_id, item_id, mood_reviews.UNRELATED))


async def correct_recommendation(submission_id: UUID, assessment_id: UUID, user_id: UUID, recommendation: str,
                                 reason_code: str, note: Optional[str], resolution: bool = False) -> None:
    """The reviewer's decision on the assessment: what it should have been (or AGREE: that it was right). With
    resolution=True it is a lead reviewer settling a disagreement between reviewers."""
    code = _check_reason(reason_code, CORRECTION_REASONS)
    await assessment_of(submission_id, assessment_id)
    await get_pool().execute(
        """
        INSERT INTO public_mood_assessment_feedback (assessment_id, kind, recommendation, reason_code, note, user_id)
        VALUES ($1, 'RECOMMENDATION_CORRECTION', $2, $3, $4, $5)
        ON CONFLICT (assessment_id, user_id) WHERE kind = 'RECOMMENDATION_CORRECTION'
        DO UPDATE SET recommendation = EXCLUDED.recommendation, reason_code = EXCLUDED.reason_code,
                      note = EXCLUDED.note, created_at = NOW()
        """,
        assessment_id, recommendation, code, (note or "").strip() or None, user_id,
    )
    await mood_reviews.mirror(mood_reviews.record_decision(
        assessment_id, submission_id, user_id, recommendation, code, note, resolution=resolution))
    mood_auto_train.schedule("review")  # a new decision is new training data


async def confirm_recommendation(submission_id: UUID, assessment_id: UUID, user_id: UUID) -> None:
    """"The recommendation is right": the reviewer's decision is the assessment's own recommendation. Without these
    the model would only ever see the cases reviewers disagreed with."""
    await assessment_of(submission_id, assessment_id)
    row = await fetch_one("SELECT result->>'recommendation' AS rec FROM public_mood_assessments WHERE id = $1", assessment_id)
    if not row or row["rec"] not in ("POST", "WAIT", "HOLD"):
        raise HTTPException(status_code=409, detail="That assessment has no recommendation to confirm.")
    await correct_recommendation(submission_id, assessment_id, user_id, row["rec"], "AGREE", None)


async def mark_wrong_sentiment(submission_id: UUID, assessment_id: UUID, item_id: UUID, user_id: UUID,
                               correct_sentiment: str, note: Optional[str]) -> None:
    """"This item reads differently": what its sentiment should be. Recorded for improving the sentiment analysis; the
    assessment is not changed by it."""
    sentiment = (correct_sentiment or "").strip().upper()
    if sentiment not in mood_reviews.SENTIMENTS:
        raise HTTPException(status_code=422, detail=f"Sentiment must be one of {', '.join(mood_reviews.SENTIMENTS)}.")
    await assessment_of(submission_id, assessment_id)
    seen = await fetch_one(
        "SELECT 1 AS ok FROM public_mood_assessment_items WHERE assessment_id = $1 AND item_id = $2", assessment_id, item_id)
    if not seen:
        raise HTTPException(status_code=404, detail="That item wasn't part of this assessment")
    await mood_reviews.record_match_correction(
        assessment_id, submission_id, user_id, mood_reviews.WRONG_SENTIMENT, item_id, correct_sentiment=sentiment, comment=note)


async def clear_wrong_sentiment(submission_id: UUID, assessment_id: UUID, item_id: UUID, user_id: UUID) -> None:
    await assessment_of(submission_id, assessment_id)
    await mood_reviews.retract_match_corrections(submission_id, item_id, mood_reviews.WRONG_SENTIMENT, user_id)


async def clear_correction(submission_id: UUID, assessment_id: UUID, user_id: UUID) -> None:
    await assessment_of(submission_id, assessment_id)
    await get_pool().execute(
        "DELETE FROM public_mood_assessment_feedback WHERE assessment_id = $1 AND user_id = $2 "
        "AND kind = 'RECOMMENDATION_CORRECTION'",
        assessment_id, user_id,
    )
    await mood_reviews.mirror(mood_reviews.retract_decision(assessment_id, user_id))
    mood_auto_train.schedule("review")  # a withdrawn decision changes the training data too


async def rejected_item_ids(submission_id: UUID) -> Set[str]:
    """Items any reviewer marked unrelated for this post (ids as strings, as the evidence code uses them)."""
    rows = await fetch_all(
        """
        SELECT DISTINCT f.item_id FROM public_mood_assessment_feedback f
        JOIN public_mood_assessments a ON a.id = f.assessment_id
        WHERE a.submission_id = $1 AND f.kind = 'MATCH_UNRELATED' AND f.item_id IS NOT NULL
        """,
        submission_id,
    )
    return {str(r["item_id"]) for r in rows}


async def excluded_for(submission_id: UUID) -> List[ExcludedMatch]:
    rows = await fetch_all(
        """
        SELECT DISTINCT ON (f.item_id) f.item_id, i.text, i.topic_name AS topic, i.url, f.reason_code, f.note,
               u.full_name AS by_name, f.created_at AS at
        FROM public_mood_assessment_feedback f
        JOIN public_mood_assessments a ON a.id = f.assessment_id
        JOIN listening_items i ON i.id = f.item_id
        LEFT JOIN users u ON u.id = f.user_id
        WHERE a.submission_id = $1 AND f.kind = 'MATCH_UNRELATED'
        ORDER BY f.item_id, f.created_at DESC
        """,
        submission_id,
    )
    return [ExcludedMatch(**dict(r)) for r in rows]


async def corrections_for(assessment_id: UUID, viewer: Optional[UUID]) -> List[ReviewerCorrection]:
    rows = await fetch_all(
        """
        SELECT f.recommendation, f.reason_code, f.note, u.full_name AS by_name, f.created_at AS at,
               (f.user_id IS NOT DISTINCT FROM $2) AS mine
        FROM public_mood_assessment_feedback f LEFT JOIN users u ON u.id = f.user_id
        WHERE f.assessment_id = $1 AND f.kind = 'RECOMMENDATION_CORRECTION'
        ORDER BY f.created_at
        """,
        assessment_id, viewer,
    )
    return [ReviewerCorrection(**dict(r)) for r in rows]
