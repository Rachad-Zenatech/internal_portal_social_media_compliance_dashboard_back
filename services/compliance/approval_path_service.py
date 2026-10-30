"""Approval paths: who gives final approval for which content.

A path matches content by brand, audience and (optionally) whether it's Shaun's own thought leadership, and names
its final approvers. When paths match, only their approvers (or a super admin) can approve; any reviewer on the
brand can still comment and send it back. Content no path matches can be approved by any of the brand's reviewers.
"""

from typing import Any, Dict, Iterable, List, Optional, Tuple
from uuid import UUID

from fastapi import HTTPException

from models.compliance.approval_path_model import ApprovalPath, ApprovalPathIn, ApprovalPathOptions, ApproverOption
from models.compliance.submission_model import AUDIENCES
from postgresql_db.database import fetch_all, fetch_one
from services.shared.audit_service import log_audit_event

CEO_RULE_SET = "Shaun CEO Thought Leadership"

_SELECT = """
    SELECT p.*,
           coalesce((SELECT array_agg(u.full_name ORDER BY u.full_name) FROM users u WHERE u.id = ANY(p.approver_ids)),
                    '{}') AS approver_names
    FROM approval_paths p
"""


def _out(r: Dict[str, Any]) -> ApprovalPath:
    return ApprovalPath(
        id=r["id"], brand_code=r["brand_code"], audience=r["audience"], ceo_content=r["ceo_content"],
        label=r["label"], approver_ids=list(r["approver_ids"] or []), approver_names=list(r["approver_names"] or []),
        created_at=r["created_at"], updated_at=r["updated_at"],
    )


async def list_paths() -> List[ApprovalPath]:
    rows = await fetch_all(_SELECT + " ORDER BY p.brand_code NULLS LAST, p.audience NULLS LAST, p.ceo_content DESC, p.created_at")
    return [_out(r) for r in rows]


async def options() -> ApprovalPathOptions:
    """Who can be named an approver (anyone who can review) and what a path can match on."""
    from services.compliance import submission_service

    ids = await submission_service._reviewer_ids()
    rows = await fetch_all(
        """
        SELECT u.id, u.full_name, u.email,
               coalesce((SELECT array_agg(ub.brand_code ORDER BY ub.brand_code) FROM user_brands ub WHERE ub.user_id = u.id),
                        '{}') AS brands
        FROM users u WHERE u.id = ANY($1::uuid[]) ORDER BY u.full_name
        """,
        ids,
    )
    brands = await fetch_all("SELECT code, name FROM brands WHERE is_active ORDER BY sort_order, name")
    return ApprovalPathOptions(
        approvers=[ApproverOption(id=r["id"], name=r["full_name"] or r["email"], email=r["email"], brands=list(r["brands"])) for r in rows],
        brands=[{"code": b["code"], "name": b["name"]} for b in brands],
        audiences=AUDIENCES,
    )


async def _validate(data: ApprovalPathIn) -> None:
    from services.compliance import submission_service

    if data.brand_code and not await fetch_one("SELECT 1 FROM brands WHERE code = $1 AND is_active", data.brand_code):
        raise HTTPException(status_code=422, detail=f"Unknown brand: {data.brand_code}")
    if data.audience and data.audience not in AUDIENCES:
        raise HTTPException(status_code=422, detail=f"Unknown audience: {data.audience}")
    # Approvers must be able to review the brand, or they'd be named for content they can't open.
    allowed = set(await submission_service._reviewer_ids(brand_code=data.brand_code))
    bad = [a for a in data.approver_ids if a not in allowed]
    if bad:
        names = await fetch_all("SELECT full_name FROM users WHERE id = ANY($1::uuid[])", bad)
        who = ", ".join(r["full_name"] for r in names) or "Some of the people chosen"
        raise HTTPException(
            status_code=422,
            detail=f"{who} can't review {'this brand' if data.brand_code else 'content'}. "
                   "Give them a reviewer role (and the brand) first.",
        )


async def create_path(data: ApprovalPathIn, user_id: UUID) -> ApprovalPath:
    await _validate(data)
    row = await fetch_one(
        """
        INSERT INTO approval_paths (brand_code, audience, ceo_content, label, approver_ids, created_by, updated_by)
        VALUES ($1, $2, $3, $4, $5, $6, $6) RETURNING id
        """,
        data.brand_code or None, data.audience or None, data.ceo_content, (data.label or "").strip() or None,
        list(dict.fromkeys(data.approver_ids)), user_id,
    )
    await log_audit_event("approval_path", str(row["id"]), "CREATE_APPROVAL_PATH", user_id, data.model_dump(mode="json"))
    return _out(await fetch_one(_SELECT + " WHERE p.id = $1", row["id"]))


async def update_path(path_id: UUID, data: ApprovalPathIn, user_id: UUID) -> ApprovalPath:
    await _validate(data)
    before = await fetch_one(_SELECT + " WHERE p.id = $1", path_id)
    if not before:
        raise HTTPException(status_code=404, detail="Approval path not found")
    await fetch_one(
        """
        UPDATE approval_paths
        SET brand_code = $2, audience = $3, ceo_content = $4, label = $5, approver_ids = $6,
            updated_by = $7, updated_at = NOW()
        WHERE id = $1 RETURNING id
        """,
        path_id, data.brand_code or None, data.audience or None, data.ceo_content, (data.label or "").strip() or None,
        list(dict.fromkeys(data.approver_ids)), user_id,
    )
    await log_audit_event("approval_path", str(path_id), "UPDATE_APPROVAL_PATH", user_id,
                          {"before": _out(before).model_dump(mode="json"), "after": data.model_dump(mode="json")})
    return _out(await fetch_one(_SELECT + " WHERE p.id = $1", path_id))


async def delete_path(path_id: UUID, user_id: UUID) -> None:
    before = await fetch_one(_SELECT + " WHERE p.id = $1", path_id)
    if not before:
        raise HTTPException(status_code=404, detail="Approval path not found")
    await fetch_one("DELETE FROM approval_paths WHERE id = $1 RETURNING id", path_id)
    await log_audit_event("approval_path", str(path_id), "DELETE_APPROVAL_PATH", user_id, _out(before).model_dump(mode="json"))


# ---------------------------------------------------------------------------
# Matching content to its approvers
# ---------------------------------------------------------------------------

class ApprovalRules:
    """All paths, loaded once per request, matched to as many submissions as needed."""

    def __init__(self, paths: List[Dict[str, Any]]):
        self.paths = paths

    def approvers(self, brand_code: Optional[str], audience: Optional[str], ceo_content: bool) -> Tuple[List[UUID], List[str]]:
        ids: Dict[UUID, str] = {}
        for p in self.paths:
            if p["brand_code"] and p["brand_code"] != brand_code:
                continue
            if p["audience"] and p["audience"] != audience:
                continue
            if p["ceo_content"] and not ceo_content:
                continue
            for uid, name in zip(p["approver_ids"] or [], p["approver_names"] or []):
                ids.setdefault(uid, name)
        return list(ids), sorted(set(ids.values()))


async def load_rules() -> ApprovalRules:
    # Only active approvers count; ids and names come from the same filtered, ordered list so they stay aligned.
    # A path whose approvers have all left names nobody, so any brand reviewer can approve again.
    rows = await fetch_all(
        """
        SELECT p.brand_code, p.audience, p.ceo_content,
               ARRAY(SELECT u.id FROM unnest(p.approver_ids) WITH ORDINALITY a(id, n)
                     JOIN users u ON u.id = a.id AND u.is_active AND u.deleted_at IS NULL ORDER BY a.n) AS approver_ids,
               ARRAY(SELECT coalesce(u.full_name, u.email) FROM unnest(p.approver_ids) WITH ORDINALITY a(id, n)
                     JOIN users u ON u.id = a.id AND u.is_active AND u.deleted_at IS NULL ORDER BY a.n) AS approver_names
        FROM approval_paths p
        """
    )
    return ApprovalRules([dict(r) for r in rows])


def is_ceo_content(rule_sets: Iterable[str], accounts: Iterable[Any]) -> bool:
    """Shaun's own thought leadership: posted through his account or checked against his rule set."""
    return CEO_RULE_SET in set(rule_sets or []) or any(getattr(a, "is_ceo", False) for a in accounts or [] if a)
