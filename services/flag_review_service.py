"""Reviewers' calls on automated compliance flags.

A reviewer can dismiss a flag on the version in review, with a reason (negated, different meaning, ...). The saved
score isn't changed: it stays what the rulebook found, and the dismissal sits beside it with who, why and when.
Before content can be approved, critical flags must be fixed or dismissed, and the score counting only the flags
still open (the approval score) must reach MIN_APPROVAL_SCORE. Dismissals per rule show how noisy each rule's
phrases are.
"""

import json
import os
from dataclasses import dataclass
from typing import Any, Dict, Iterable, List, Optional, Set
from uuid import UUID

from fastapi import HTTPException

from models.flag_review_model import REASON_LABELS, FlagDismissRequest, FlagReview, RuleFlagStats
from postgresql_db.database import fetch_all, fetch_one, get_pool
from services.audit_service import log_audit_event
from services.compliance_service import CRITICAL_SCORE_CAP

# The least rulebook score (counting only flags nobody has dismissed) content needs to be approved.
MIN_APPROVAL_SCORE = int(os.getenv("MIN_APPROVAL_SCORE", "70"))


def _json(value: Any) -> Any:
    return json.loads(value) if isinstance(value, str) else value


def _out(r: Dict[str, Any]) -> FlagReview:
    return FlagReview(
        rule_id=r["rule_id"], rule_number=r["rule_number"], version_number=r["version_number"],
        flagged_terms=list(r["flagged_terms"] or []), traffic_light=r["traffic_light"], reason=r["reason"],
        reason_label=REASON_LABELS.get(r["reason"], r["reason"]), note=r["note"],
        decided_by_name=r.get("decided_by_name"), decided_at=r["decided_at"],
    )


_SELECT = """
    SELECT f.*, u.full_name AS decided_by_name
    FROM flag_reviews f LEFT JOIN users u ON u.id = f.decided_by
"""


async def list_for(submission_id: UUID, user_id: UUID) -> List[FlagReview]:
    from services import submission_service

    await submission_service._get_owned_row(submission_id, user_id, manager_can_read=True)
    rows = await fetch_all(_SELECT + " WHERE f.submission_id = $1 ORDER BY f.version_number DESC, f.decided_at", submission_id)
    return [_out(r) for r in rows]


async def _reviewable(submission_id: UUID, user_id: UUID) -> Dict[str, Any]:
    """The submission, if this user may make calls on its flags now: a reviewer, while it's in review."""
    from services import submission_service

    row = await submission_service._get_owned_row(submission_id, user_id, manager_can_read=True)
    if not await submission_service._can_review(user_id):
        raise HTTPException(status_code=403, detail="Only reviewers can dismiss compliance flags")
    if row["status"] != "IN_REVIEW":
        raise HTTPException(status_code=409, detail="Flags can only be dismissed while the content is in review")
    if row["created_by"] == user_id and not await submission_service._is_super_admin(user_id):
        raise HTTPException(status_code=403, detail="You can't dismiss flags on your own content; another reviewer needs to")
    return row


async def dismiss(submission_id: UUID, rule_id: UUID, user_id: UUID, req: FlagDismissRequest) -> FlagReview:
    row = await _reviewable(submission_id, user_id)
    check = _json(row["latest_check"]) or {}
    v = next((v for v in check.get("violations") or [] if str(v.get("rule_id")) == str(rule_id)), None)
    if not v:
        raise HTTPException(status_code=404, detail="That rule isn't flagged on the version in review")
    note = (req.note or "").strip() or None
    if req.reason == "OTHER" and not note:
        raise HTTPException(status_code=422, detail="Say why the flag doesn't apply")
    pool = get_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            await conn.execute(
                """
                INSERT INTO flag_reviews
                    (submission_id, version_number, rule_id, rule_number, flagged_terms, traffic_light, reason, note, decided_by)
                VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9)
                ON CONFLICT (submission_id, version_number, rule_id) DO UPDATE SET
                    reason = EXCLUDED.reason, note = EXCLUDED.note, decided_by = EXCLUDED.decided_by, decided_at = NOW()
                """,
                submission_id, row["current_version"], rule_id, v.get("rule_number"), list(v.get("flagged_terms") or []),
                v.get("traffic_light") or "YELLOW", req.reason, note, user_id,
            )
            from services import submission_service

            await submission_service._add_event(
                conn, submission_id, "FLAG_DISMISSED",
                f"{v.get('rule_number')} flag dismissed: {REASON_LABELS[req.reason]}" + (f" ({note})" if note else ""),
                user_id, {"rule_id": str(rule_id), "reason": req.reason, "note": note, "version": row["current_version"]},
            )
    await log_audit_event("content_submission", str(submission_id), "DISMISS_COMPLIANCE_FLAG", user_id,
                          {"rule_number": v.get("rule_number"), "flagged_terms": v.get("flagged_terms"),
                           "reason": req.reason, "note": note, "version": row["current_version"]})
    saved = await fetch_one(
        _SELECT + " WHERE f.submission_id = $1 AND f.version_number = $2 AND f.rule_id = $3",
        submission_id, row["current_version"], rule_id,
    )
    return _out(saved)


async def undo(submission_id: UUID, rule_id: UUID, user_id: UUID) -> None:
    row = await _reviewable(submission_id, user_id)
    removed = await fetch_one(
        "DELETE FROM flag_reviews WHERE submission_id = $1 AND version_number = $2 AND rule_id = $3 RETURNING rule_number",
        submission_id, row["current_version"], rule_id,
    )
    if not removed:
        raise HTTPException(status_code=404, detail="That flag isn't dismissed")
    pool = get_pool()
    async with pool.acquire() as conn:
        from services import submission_service

        await submission_service._add_event(conn, submission_id, "FLAG_RESTORED",
                                            f"{removed['rule_number']} flag restored", user_id, {"rule_id": str(rule_id)})
    await log_audit_event("content_submission", str(submission_id), "RESTORE_COMPLIANCE_FLAG", user_id,
                          {"rule_number": removed["rule_number"], "version": row["current_version"]})


def score_after_dismissals(check: Optional[Dict[str, Any]], dismissed: Set[str]) -> Optional[int]:
    """The rulebook score counting only the flags nobody has dismissed (by rule id): each dismissed flag gives its
    points back, and the critical cap only holds while a critical flag is still open. None when there's no score
    yet (not checked, or still being analysed)."""
    if not check or check.get("score") is None or check.get("analysis_pending"):
        return None
    violations = check.get("violations") or []
    # Checks saved before findings carried their points can't be recounted; they keep their score.
    if any("penalty" not in v for v in violations):
        return int(check["score"])
    open_flags = [v for v in violations if str(v.get("rule_id")) not in dismissed]
    dismissed_numbers = {v.get("rule_number") for v in violations if str(v.get("rule_id")) in dismissed}
    points = 0
    for v in open_flags:
        # Words counted once under a stricter rule cost their own points if that rule's flag was dismissed.
        if v.get("counted_with") and v["counted_with"] in dismissed_numbers:
            points += int(v.get("uncounted_penalty") or 0)
        else:
            points += int(v.get("penalty") or 0)
    score = max(0, 100 - points)
    if any(v.get("traffic_light") == "RED" for v in open_flags):
        score = min(score, CRITICAL_SCORE_CAP)
    return score


@dataclass(frozen=True)
class ApprovalStanding:
    # Critical (red) flags on the current version that nobody has dismissed.
    open_critical: int
    # The score counting only open flags (see score_after_dismissals); None when there's none yet.
    score: Optional[int]
    min_score: int = MIN_APPROVAL_SCORE

    @property
    def score_too_low(self) -> bool:
        return self.score is not None and self.score < self.min_score


async def approval_standing(rows: Iterable[Dict[str, Any]]) -> Dict[UUID, ApprovalStanding]:
    """Per submission, what stands between its current version and approval: open critical flags and the score
    counting only open flags. One query for all."""
    rows = list(rows)
    if not rows:
        return {}
    dismissed = await fetch_all(
        """
        SELECT f.submission_id, f.rule_id FROM flag_reviews f
        JOIN content_submissions s ON s.id = f.submission_id AND s.current_version = f.version_number
        WHERE f.submission_id = ANY($1::uuid[])
        """,
        [r["id"] for r in rows],
    )
    done: Dict[UUID, Set[str]] = {}
    for d in dismissed:
        done.setdefault(d["submission_id"], set()).add(str(d["rule_id"]))
    out: Dict[UUID, ApprovalStanding] = {}
    for r in rows:
        check = _json(r.get("latest_check")) or {}
        mine = done.get(r["id"], set())
        out[r["id"]] = ApprovalStanding(
            open_critical=sum(
                1 for v in check.get("violations") or []
                if v.get("traffic_light") == "RED" and str(v.get("rule_id")) not in mine
            ),
            score=score_after_dismissals(check, mine),
        )
    return out


async def open_critical_counts(rows: Iterable[Dict[str, Any]]) -> Dict[UUID, int]:
    """Per submission: critical (red) flags on its current version that nobody has dismissed. One query for all."""
    return {k: s.open_critical for k, s in (await approval_standing(rows)).items()}


async def rule_stats() -> List[RuleFlagStats]:
    """How often each rule flagged a submitted version, and how often reviewers dismissed it."""
    flagged = await fetch_all(
        """
        SELECT v->>'rule_number' AS rule_number, count(DISTINCT (j.submission_id, j.version_number)) AS n
        FROM content_review_jobs j
        CROSS JOIN LATERAL jsonb_array_elements(coalesce(j.automated_check->'violations', '[]'::jsonb)) v
        GROUP BY 1
        """
    )
    dismissed = await fetch_all(
        """
        SELECT rule_number, count(*) AS n, mode() WITHIN GROUP (ORDER BY reason) AS top_reason
        FROM flag_reviews GROUP BY rule_number
        """
    )
    by_rule: Dict[str, RuleFlagStats] = {}
    for r in flagged:
        if r["rule_number"]:
            by_rule[r["rule_number"]] = RuleFlagStats(rule_number=r["rule_number"], flagged=r["n"])
    for r in dismissed:
        s = by_rule.setdefault(r["rule_number"], RuleFlagStats(rule_number=r["rule_number"]))
        s.dismissed = r["n"]
        s.top_reason = r["top_reason"]
        s.top_reason_label = REASON_LABELS.get(r["top_reason"], r["top_reason"])
    for s in by_rule.values():
        s.dismissal_rate = round(s.dismissed / s.flagged, 2) if s.flagged else None
    return sorted(by_rule.values(), key=lambda s: s.rule_number)
