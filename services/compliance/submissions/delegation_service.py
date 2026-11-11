"""Review delegation: a reviewer hands a post in review to someone else.

The delegate is added to the post's reviewers, so it shows in their queue and they can decide on it. The delegator
stays on it: they keep seeing its status and can still decide. One approval from anyone on it approves it. Every
hand-off is kept, for the Delegations page.
"""

from typing import Any, Dict, List, Optional, Tuple
from uuid import UUID

from fastapi import HTTPException

from models.compliance.submission_model import DelegateRequest, Delegation, DelegationDirection, Submission
from postgresql_db.database import fetch_all, get_pool
from services.shared.core.audit_service import log_audit_event


def extend(approver_ids: List[UUID], edges: List[Tuple[UUID, UUID]]) -> List[UUID]:
    """Named final approvers plus whoever they delegated the post to (and on down the chain)."""
    result = list(approver_ids)
    if not result:
        return result
    changed = True
    while changed:
        changed = False
        for frm, to in edges:
            if frm in result and to and to not in result:
                result.append(to)
                changed = True
    return result


async def edges_for(submission_ids: List[UUID]) -> Dict[UUID, List[Tuple[UUID, UUID]]]:
    """Each post's hand-offs as (from, to) pairs, for many posts in one query."""
    if not submission_ids:
        return {}
    rows = await fetch_all(
        "SELECT submission_id, from_user_id, to_user_id FROM content_review_delegations "
        "WHERE submission_id = ANY($1::uuid[]) ORDER BY created_at",
        list(submission_ids),
    )
    out: Dict[UUID, List[Tuple[UUID, UUID]]] = {}
    for r in rows:
        out.setdefault(r["submission_id"], []).append((r["from_user_id"], r["to_user_id"]))
    return out


async def with_delegates(submission_id: UUID, approver_ids: List[UUID]) -> List[UUID]:
    if not approver_ids:
        return approver_ids
    return extend(approver_ids, (await edges_for([submission_id])).get(submission_id, []))


async def has_delegated(submission_id: UUID, version: int, user_id: UUID) -> bool:
    """Whether this person already delegated this version of the post (each person delegates a version once)."""
    rows = await fetch_all(
        "SELECT 1 FROM content_review_delegations WHERE submission_id = $1 AND version_number = $2 AND from_user_id = $3",
        submission_id, version, user_id,
    )
    return bool(rows)


async def delegated_by(user_id: UUID, versions: Dict[UUID, int]) -> set:
    """Of these posts ({id: current version}), the ones this person already delegated at that version."""
    if not versions:
        return set()
    rows = await fetch_all(
        "SELECT submission_id, version_number FROM content_review_delegations "
        "WHERE from_user_id = $1 AND submission_id = ANY($2::uuid[])",
        user_id, list(versions),
    )
    return {r["submission_id"] for r in rows if versions.get(r["submission_id"]) == r["version_number"]}


async def delegate(submission_id: UUID, user_id: UUID, req: DelegateRequest) -> Submission:
    from services.compliance.submissions import submission_service as subs

    row = await subs._get_owned_row(submission_id, user_id, manager_can_read=True)
    if not await subs._can_review(user_id):
        raise HTTPException(status_code=403, detail="Only reviewers can delegate a review")
    if row["status"] != "IN_REVIEW":
        raise HTTPException(status_code=409, detail="Only content that's in review can be delegated")
    if row["created_by"] == user_id and not await subs._is_super_admin(user_id):
        raise HTTPException(status_code=403, detail="You can't delegate the review of your own content")
    if req.to_user_id == user_id:
        raise HTTPException(status_code=422, detail="Pick someone other than yourself")
    # Any reviewer can take it over, whatever brands they work on (they can open it once it's delegated to them).
    eligible = await subs._reviewer_ids(exclude=await subs._creator_exclusion(row["created_by"]))
    if req.to_user_id not in eligible:
        raise HTTPException(status_code=422, detail="That person can't review content")
    # No passing it around: each person delegates a version once (and it can never go back to anyone on it, below).
    if await has_delegated(submission_id, row["current_version"], user_id):
        raise HTTPException(status_code=409,
                            detail="You've already delegated this post. Review it yourself, or wait for the delegate.")
    if req.to_user_id in (row.get("reviewer_ids") or []):
        raise HTTPException(status_code=409, detail="That person already has this post to review")

    note = (req.note or "").strip() or None
    version = row["current_version"]
    pool = get_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            current = await conn.fetchrow(
                "SELECT status, reviewer_ids FROM content_submissions WHERE id = $1 FOR UPDATE", submission_id
            )
            if current["status"] != "IN_REVIEW":
                raise HTTPException(status_code=409, detail="Someone just decided on this; refresh to see it")
            reviewers = list(current["reviewer_ids"] or [])
            # Sent to no one in particular: the delegator takes it on, so they keep seeing it once it's narrowed.
            if not reviewers:
                reviewers.append(user_id)
            elif user_id not in reviewers:
                # A super admin who wasn't on it: they stay on it too.
                reviewers.append(user_id)
            reviewers.append(req.to_user_id)
            await conn.execute(
                "UPDATE content_submissions SET reviewer_ids = $2::uuid[], updated_at = NOW() WHERE id = $1",
                submission_id, reviewers,
            )
            names = {
                r["id"]: r["full_name"] or r["email"]
                for r in await conn.fetch("SELECT id, full_name, email FROM users WHERE id = ANY($1::uuid[])",
                                          [user_id, req.to_user_id])
            }
            await conn.execute(
                """
                INSERT INTO content_review_delegations (submission_id, version_number, from_user_id, to_user_id, note)
                VALUES ($1, $2, $3, $4, $5)
                """,
                submission_id, version, user_id, req.to_user_id, note,
            )
            await subs._add_event(
                conn, submission_id, "DELEGATED",
                f"Version {version}: review delegated by {names.get(user_id, 'a reviewer')} "
                f"to {names.get(req.to_user_id, 'another reviewer')}",
                user_id,
                {"version": version, "from_user_id": str(user_id), "to_user_id": str(req.to_user_id), "note": note},
            )

    await log_audit_event("content_submission", str(submission_id), "REVIEW_DELEGATED", user_id,
                          {"reference_code": row["reference_code"], "version": version,
                           "to_user_id": str(req.to_user_id), "note": note})
    who = names.get(user_id, "A reviewer")
    await subs._notify(
        [req.to_user_id],
        f"{row['reference_code']} was delegated to you",
        f"{who} asked you to review \"{row['title']}\" (version {version})." + (f" \"{note}\"" if note else ""),
        f"/submissions/{submission_id}",
        submission_id,
    )
    return await subs.get_submission(submission_id, user_id)


_SELECT = """
    SELECT d.id, d.submission_id, d.version_number, d.from_user_id, d.to_user_id, d.note, d.created_at,
           s.reference_code, s.title, s.content_type, s.status, s.current_version,
           fu.full_name AS from_user_name, tu.full_name AS to_user_name,
           j.decision, j.decided_at, du.full_name AS decided_by_name
    FROM content_review_delegations d
    JOIN content_submissions s ON s.id = d.submission_id AND s.deleted_at IS NULL
    LEFT JOIN users fu ON fu.id = d.from_user_id
    LEFT JOIN users tu ON tu.id = d.to_user_id
    LEFT JOIN content_review_jobs j ON j.submission_id = d.submission_id AND j.version_number = d.version_number
    LEFT JOIN users du ON du.id = j.decided_by
"""


def _out(r: Dict[str, Any]) -> Delegation:
    return Delegation(**{k: r[k] for k in Delegation.model_fields if k in r.keys()})


async def list_delegations(user_id: UUID, direction: DelegationDirection = "all") -> List[Delegation]:
    """Hand-offs the user made or received, newest first, with where each post stands now."""
    where = {
        "by_me": "d.from_user_id = $1",
        "to_me": "d.to_user_id = $1",
        "all": "(d.from_user_id = $1 OR d.to_user_id = $1)",
    }[direction]
    rows = await fetch_all(_SELECT + f" WHERE {where} ORDER BY d.created_at DESC LIMIT 500", user_id)
    return [_out(r) for r in rows]


async def submission_delegations(submission_id: UUID, user_id: UUID) -> List[Delegation]:
    """Every hand-off on one post, for anyone who can open it."""
    from services.compliance.submissions import submission_service as subs

    await subs._get_owned_row(submission_id, user_id, manager_can_read=True)
    rows = await fetch_all(_SELECT + " WHERE d.submission_id = $1 ORDER BY d.created_at DESC", submission_id)
    return [_out(r) for r in rows]


async def delegation_count(user_id: UUID) -> Optional[int]:
    """Delegated-to-me posts still waiting for a decision, for the sidebar badge."""
    rows = await fetch_all(
        """
        SELECT count(DISTINCT d.submission_id) AS n FROM content_review_delegations d
        JOIN content_submissions s ON s.id = d.submission_id AND s.deleted_at IS NULL
        WHERE d.to_user_id = $1 AND s.status = 'IN_REVIEW' AND d.version_number = s.current_version
        """,
        user_id,
    )
    return rows[0]["n"] if rows else 0
