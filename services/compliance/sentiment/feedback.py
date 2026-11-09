"""Reviewer feedback and test case evaluation for sentiment analysis."""

import json
from typing import Any, Dict, List, Optional
from uuid import UUID
from fastapi import HTTPException

from models.compliance.sentiment_model import EvalCase, EvalCaseIn, FeedbackSummaryRow, FindingFeedback, FindingFeedbackIn
from postgresql_db.database import fetch_all, fetch_one
from services.compliance.submissions import review_memory_service
from services.compliance.sentiment.config import PROMPT_VERSION
from services.compliance.sentiment.input_builder import _json

# ---------------------------------------------------------------------------
# Reviewer feedback on findings
# ---------------------------------------------------------------------------

async def _feedback_map(analysis_id: UUID, viewer: Optional[UUID]) -> Dict[str, FindingFeedback]:
    rows = await fetch_all(
        """
        SELECT finding_id,
               count(*) FILTER (WHERE verdict = 'USEFUL') AS useful,
               count(*) FILTER (WHERE verdict = 'NOT_AN_ISSUE') AS not_an_issue,
               max(verdict) FILTER (WHERE user_id = $2) AS mine,
               max(note) FILTER (WHERE user_id = $2) AS my_note
        FROM content_sentiment_feedback WHERE analysis_id = $1
        GROUP BY finding_id
        """,
        analysis_id, viewer,
    )
    return {
        r["finding_id"]: FindingFeedback(useful=r["useful"], not_an_issue=r["not_an_issue"], mine=r["mine"], my_note=r["my_note"])
        for r in rows
    }


async def set_feedback(submission_id: UUID, analysis_id: UUID, fid: str, user_id: UUID,
                       data: FindingFeedbackIn) -> Dict[str, FindingFeedback]:
    """Record (or change) the reviewer's verdict on one finding."""
    # jobs imports this module, so it is imported when needed.
    from services.compliance.sentiment.jobs import _analysis_row, _result

    row = await _analysis_row(submission_id, analysis_id)
    result = _result(row)
    finding = next((f for f in (result.findings if result else []) if f.id == fid), None)
    if not finding:
        raise HTTPException(status_code=404, detail="That finding isn't in this analysis")
    note = (data.note or "").strip() or None
    await fetch_one(
        """
        INSERT INTO content_sentiment_feedback (analysis_id, finding_id, user_id, verdict, note, category, basis, quote)
        VALUES ($1, $2, $3, $4, $5, $6, $7, $8)
        ON CONFLICT (analysis_id, finding_id, user_id) DO UPDATE SET
            verdict = EXCLUDED.verdict, note = EXCLUDED.note, updated_at = NOW()
        RETURNING finding_id
        """,
        analysis_id, fid, user_id, data.verdict, note, finding.category, finding.basis, finding.quote,
    )
    # A rating on an already-decided version updates what review memory knows about it.
    review_memory_service.remember_soon(submission_id, row["version_number"])
    return await _feedback_map(analysis_id, user_id)


async def clear_feedback(submission_id: UUID, analysis_id: UUID, fid: str, user_id: UUID) -> Dict[str, FindingFeedback]:
    from services.compliance.sentiment.jobs import _analysis_row

    await _analysis_row(submission_id, analysis_id)
    await fetch_one(
        "DELETE FROM content_sentiment_feedback WHERE analysis_id = $1 AND finding_id = $2 AND user_id = $3 RETURNING finding_id",
        analysis_id, fid, user_id,
    )
    return await _feedback_map(analysis_id, user_id)


async def feedback_summary() -> List[FeedbackSummaryRow]:
    """Which kinds of finding reviewers find useful or dismiss, noisiest first."""
    rows = await fetch_all(
        """
        SELECT category, basis, count(*) AS rated,
               count(*) FILTER (WHERE verdict = 'USEFUL') AS useful,
               count(*) FILTER (WHERE verdict = 'NOT_AN_ISSUE') AS not_an_issue
        FROM content_sentiment_feedback
        GROUP BY category, basis
        ORDER BY count(*) FILTER (WHERE verdict = 'NOT_AN_ISSUE')::float / count(*) DESC, count(*) DESC
        """
    )
    return [FeedbackSummaryRow(**dict(r)) for r in rows]


# ---------------------------------------------------------------------------
# Test set
# ---------------------------------------------------------------------------

_CASE_SELECT = """
    SELECT e.*, s.reference_code, s.title, u.full_name AS created_by_name
    FROM sentiment_eval_cases e
    LEFT JOIN content_submissions s ON s.id = e.submission_id
    LEFT JOIN users u ON u.id = e.created_by
"""


def _case_out(r: Dict[str, Any]) -> EvalCase:
    return EvalCase(
        id=r["id"], submission_id=r["submission_id"], reference_code=r["reference_code"], title=r["title"],
        expected_assessment=r["expected_assessment"], expected_concern=r["expected_concern"],
        must_flag=list(r["must_flag"] or []), must_not_flag=list(r["must_not_flag"] or []),
        notes=r["notes"], created_by_name=r["created_by_name"], created_at=r["created_at"],
    )


async def create_eval_case(data: EvalCaseIn, user_id: UUID) -> EvalCase:
    """Freeze an analysed post as a test case. What it should and shouldn't flag comes from reviewers' feedback
    on that analysis's findings."""
    row = await fetch_one(
        "SELECT submission_id, input FROM content_sentiment_analyses WHERE id = $1 AND status = 'COMPLETED'",
        data.analysis_id,
    )
    if not row or not row["input"]:
        raise HTTPException(status_code=422, detail="Only a completed analysis can be saved as a test case")
    saved = await fetch_one(
        """
        INSERT INTO sentiment_eval_cases
            (analysis_id, submission_id, input, expected_assessment, expected_concern, must_flag, must_not_flag, notes, created_by)
        SELECT $1, $2, $3::jsonb, $4, $5,
               coalesce(array_agg(DISTINCT f.quote) FILTER (WHERE f.verdict = 'USEFUL'), '{}'),
               coalesce(array_agg(DISTINCT f.quote) FILTER (WHERE f.verdict = 'NOT_AN_ISSUE'), '{}'),
               $6, $7
        FROM (SELECT 1) one
        LEFT JOIN content_sentiment_feedback f ON f.analysis_id = $1
        RETURNING id
        """,
        data.analysis_id, row["submission_id"], json.dumps(_json(row["input"]), default=str),
        data.expected_assessment, data.expected_concern, (data.notes or "").strip() or None, user_id,
    )
    return _case_out(await fetch_one(_CASE_SELECT + " WHERE e.id = $1", saved["id"]))


async def list_eval_cases() -> List[EvalCase]:
    return [_case_out(r) for r in await fetch_all(_CASE_SELECT + " ORDER BY e.created_at DESC")]


async def delete_eval_case(case_id: UUID) -> None:
    if not await fetch_one("DELETE FROM sentiment_eval_cases WHERE id = $1 RETURNING id", case_id):
        raise HTTPException(status_code=404, detail="Test case not found")

