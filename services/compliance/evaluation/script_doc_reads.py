"""Script documents: the lists and reads (the hub's list, the history of a script, the rulebook check)."""

from typing import Any, Dict, List, Optional
from uuid import UUID

from fastapi import HTTPException

from models.compliance.script_doc_model import DocEvent, DocHistory, DocSummary, DocVersion
from postgresql_db.database import fetch_all
from services.compliance.evaluation import script_doc_service as core
from services.compliance.evaluation import script_review_service as scripts
from services.compliance.submissions import submission_service as subs


async def list_docs(user_id: UUID, scope: str = "mine") -> List[DocSummary]:
    """The hub's list. Creators see their own; reviewers the ones sent to them; Content Managers every script."""
    manager = await subs._can_manage_team_content(user_id)
    if scope == "all" and not manager:
        raise HTTPException(status_code=403, detail="Only Content Managers see every script")
    if scope == "all":
        where, args = "TRUE", []
    elif scope == "review":
        where, args = "EXISTS (SELECT 1 FROM script_document_reviewers rr WHERE rr.document_id = d.id AND rr.user_id = $1)", [user_id]
    else:
        where, args = "d.created_by = $1", [user_id]
    rows = await fetch_all(
        f"""
        SELECT d.id, d.title, d.template_key, d.status, d.round, d.created_by, d.updated_at, d.created_at,
               coalesce(nullif(u.full_name, ''), u.email) AS creator_name,
               coalesce((SELECT array_agg(coalesce(nullif(ru.full_name, ''), ru.email) ORDER BY r.position)
                         FROM script_document_reviewers r JOIN users ru ON ru.id = r.user_id WHERE r.document_id = d.id), '{{}}') AS reviewer_names,
               (SELECT count(*) FROM script_document_reviewers r WHERE r.document_id = d.id AND r.capacity = 'EDIT') AS required,
               (SELECT count(*) FROM script_document_checkins c JOIN script_document_reviewers r
                  ON r.document_id = c.document_id AND r.user_id = c.user_id AND r.capacity = 'EDIT'
                WHERE c.document_id = d.id AND c.round = d.round) AS checked,
               EXISTS (SELECT 1 FROM script_document_reviewers rr WHERE rr.document_id = d.id AND rr.user_id = $1
                       AND rr.capacity = 'EDIT'
                       AND NOT EXISTS (SELECT 1 FROM script_document_checkins c WHERE c.document_id = d.id AND c.round = d.round AND c.user_id = $1)) AS awaiting_me
        FROM script_documents d JOIN users u ON u.id = d.created_by
        WHERE d.deleted_at IS NULL AND {where}
        ORDER BY d.updated_at DESC LIMIT 300
        """,
        *(args or [user_id]),
    )
    out = []
    for r in rows:
        mine = r["created_by"] == user_id
        needs = (
            (r["status"] == "IN_REVIEW" and r["awaiting_me"])
            or (r["status"] == "LEVEL2_REVIEW" and manager and not mine)
            or (r["status"] in ("CHANGES_REQUESTED", "APPROVED") and mine)
        )
        out.append(DocSummary(
            id=r["id"], title=r["title"], template_key=r["template_key"], status=r["status"], round=r["round"],
            creator_id=r["created_by"], creator_name=r["creator_name"], reviewer_names=list(r["reviewer_names"] or []),
            checked_in=r["checked"], required=r["required"], needs_you=bool(needs), updated_at=r["updated_at"], created_at=r["created_at"],
        ))
    return out


async def history(doc_id: UUID, user_id: UUID) -> DocHistory:
    await core.get_access(doc_id, user_id)
    events = await fetch_all(
        "SELECT id, round, user_name, kind, detail, created_at FROM script_document_events WHERE document_id = $1 ORDER BY id DESC LIMIT 300", doc_id)
    versions = await fetch_all(
        "SELECT id, round, label, body_html, body_text, created_by_name, created_at FROM script_document_versions "
        "WHERE document_id = $1 ORDER BY id DESC LIMIT 50", doc_id)
    return DocHistory(
        events=[DocEvent(**{**e, "detail": subs._json(e["detail"]) or {}}) for e in events],
        versions=[DocVersion(**v) for v in versions])


async def check(doc_id: UUID, user_id: UUID, text: Optional[str]) -> Dict[str, Any]:
    """The rulebook check of the script as it reads now (or the text given), judged by the script's details."""
    doc, reviewers, access = await core.get_access(doc_id, user_id)
    details = subs._json(doc["details"]) or {}
    row = {
        "title": doc["title"], "content_type": "VIDEO", "platforms": details.get("platforms") or [],
        "rule_sets": details.get("rule_sets") or [], "brand_codes": details.get("brand_codes") or [],
        "audiences": details.get("audiences") or [], "region": details.get("region"),
    }
    body = (text if text is not None else doc["body_text"]) or ""
    if not body.strip():
        raise HTTPException(status_code=400, detail="The script is empty")
    return await scripts.check_text(row, body.replace("\r\n", "\n").strip())
