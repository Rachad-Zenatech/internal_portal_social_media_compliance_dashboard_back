"""Script stage of a video post.

The creator uploads the script (PDF, Word or text) before filming. Its text is read, checked against the rulebook and
sent to a reviewer, who can approve it, send it back with a note, or edit the wording themselves. Edited scripts go back
to the creator, who sees both versions and accepts the reviewer's or edits it and sends it again. The video can only be
uploaded once the script is approved, and the approved wording is what the video's own check uses.

    DRAFT -> IN_REVIEW -> APPROVED
                       -> CHANGES_REQUESTED (note only)   -> creator edits or uploads a new file -> IN_REVIEW
                       -> EDITS_PROPOSED (reviewer edits) -> creator accepts -> APPROVED
                                                          -> creator edits   -> IN_REVIEW
"""

import asyncio
import json
import logging
import re
from typing import Any, Dict, List, Optional
from uuid import UUID

from fastapi import HTTPException

from models.compliance.script_review_model import (
    ScriptDecisionRequest,
    ScriptQueueItem,
    ScriptReviseRequest,
    ScriptStage,
    ScriptVersion,
)
from postgresql_db.database import execute, fetch_all, fetch_one, get_pool
from services.compliance import document_layout, document_text_service, submission_service as subs
from services.compliance.compliance_service import check_compliance, rulebook_version
from services.shared.audit_service import log_audit_event

logger = logging.getLogger(__name__)

# Where the creator can still change the script, and where it waits on the creator.
CREATOR_TURN = ("EDITS_PROPOSED", "CHANGES_REQUESTED")


def _same(a: str, b: str) -> bool:
    """Whether two versions say the same thing (spacing and line endings aside)."""
    norm = lambda t: re.sub(r"\s+", " ", (t or "").replace("\r", "")).strip()
    return norm(a) == norm(b)


def _clean(text: str) -> str:
    return (text or "").replace("\r\n", "\n").strip()


def _status(row: Dict[str, Any]) -> str:
    return row.get("script_status") or "DRAFT"


async def check_text(row: Dict[str, Any], text: str) -> Dict[str, Any]:
    """The rulebook check of a script: the title and the script, scoped like the post. The script is what the video
    will say and show, so rules for video apply."""
    body = "\n\n".join(p for p in [(row["title"] or "").strip(), document_layout.plain_text(text).strip()] if p)
    result = await check_compliance(subs.check_request(row, body, has_video=True, has_images=False))
    return result.model_dump(mode="json")


async def _versions(submission_id: UUID) -> List[ScriptVersion]:
    rows = await fetch_all(
        """
        SELECT v.*, cu.full_name AS created_by_name, ru.full_name AS reviewed_by_name, f.original_name AS file_name
        FROM content_script_versions v
        LEFT JOIN users cu ON cu.id = v.created_by
        LEFT JOIN users ru ON ru.id = v.reviewed_by
        LEFT JOIN content_submission_files f ON f.id = v.file_id
        WHERE v.submission_id = $1
        ORDER BY v.version_number
        """,
        submission_id,
    )
    return [
        ScriptVersion(
            version_number=r["version_number"], body=r["body"], source=r["source"], file_name=r["file_name"],
            check=subs._json(r["check_result"]), created_by=r["created_by"], created_by_name=r["created_by_name"],
            created_at=r["created_at"], review_decision=r["review_decision"], review_note=r["review_note"],
            reviewed_by_name=r["reviewed_by_name"], reviewed_at=r["reviewed_at"],
        )
        for r in rows
    ]


async def _script_file(submission_id: UUID) -> Optional[Dict[str, Any]]:
    return await fetch_one(
        """
        SELECT id, original_name, doc_text, doc_text_status, doc_text_error FROM content_submission_files
        WHERE submission_id = $1 AND kind = 'SCRIPT' AND deleted_at IS NULL
        ORDER BY created_at DESC LIMIT 1
        """,
        submission_id,
    )


def _require_stage(row: Dict[str, Any]) -> None:
    if not subs.script_stage_on(row):
        raise HTTPException(status_code=409, detail="This post doesn't have a script stage")


async def _reviewer_may_act(row: Dict[str, Any], user_id: UUID) -> bool:
    if not await subs._can_review(user_id):
        return False
    return row["created_by"] != user_id or await subs._is_super_admin(user_id)


async def get_stage(submission_id: UUID, user_id: UUID) -> ScriptStage:
    row = await subs._get_owned_row(submission_id, user_id, manager_can_read=True)
    status = _status(row) if subs.script_stage_on(row) else None
    file = await _script_file(submission_id)
    is_creator = row["created_by"] == user_id or await subs._is_super_admin(user_id)
    editable = row["status"] in subs.EDITABLE_STATUSES
    versions = await _versions(submission_id)
    if versions and status in ("IN_REVIEW", "DRAFT", "CHANGES_REQUESTED", "EDITS_PROPOSED"):
        latest = versions[-1]
        current_rb = await rulebook_version()
        if current_rb and (latest.check or {}).get("rulebook_version") != current_rb:
            fresh = await check_text(row, latest.body)
            await execute(
                "UPDATE content_script_versions SET check_result = $3::jsonb WHERE submission_id = $1 AND version_number = $2",
                submission_id, latest.version_number, json.dumps(fresh),
            )
            latest.check = fresh
    return ScriptStage(
        submission_id=row["id"],
        reference_code=row["reference_code"],
        title=row["title"],
        required=subs.script_stage_on(row),
        status=status,
        versions=versions,
        file_name=file["original_name"] if file else None,
        file_status=file["doc_text_status"] if file else None,
        file_error=file["doc_text_error"] if file else None,
        file_text=_clean(file["doc_text"]) if file and file.get("doc_text") else None,
        file_url=f"/api/submissions/{submission_id}/files/{file['id']}" if file else None,
        can_submit=bool(status) and is_creator and editable and status in ("DRAFT", "CHANGES_REQUESTED"),
        can_review=status == "IN_REVIEW" and await _reviewer_may_act(row, user_id),
        can_respond=bool(status) and is_creator and editable and status in CREATOR_TURN,
    )


async def _add_version(conn, row: Dict[str, Any], body: str, source: str, user_id: UUID,
                       check: Dict[str, Any], file_id: Optional[UUID] = None) -> int:
    number = await conn.fetchval(
        "SELECT coalesce(max(version_number), 0) + 1 FROM content_script_versions WHERE submission_id = $1", row["id"]
    )
    await conn.execute(
        """
        INSERT INTO content_script_versions (submission_id, version_number, body, source, file_id, check_result, created_by)
        VALUES ($1, $2, $3, $4, $5, $6::jsonb, $7)
        """,
        row["id"], number, body, source, file_id, json.dumps(check), user_id,
    )
    return number


async def _lock_status(conn, submission_id: UUID, allowed: tuple) -> None:
    """Lock the post and make sure the script is still where the caller saw it (two people can act at once)."""
    current = await conn.fetchval("SELECT script_status FROM content_submissions WHERE id = $1 FOR UPDATE", submission_id)
    if (current or "DRAFT") not in allowed:
        raise HTTPException(status_code=409, detail="The script's status just changed; refresh to see it")


async def _send_to_reviewers(row: Dict[str, Any], version: int, check: Dict[str, Any], user_id: UUID) -> None:
    reviewers = await subs._reviewer_ids(exclude=user_id, brand_code=row["brand_code"])
    issues = check.get("violations_count") or 0
    await subs._notify(
        reviewers,
        f"Script to review: {row['reference_code']}",
        f"\"{row['title']}\" script version {version}: score {check.get('score')}/100, "
        f"{issues} issue{'' if issues == 1 else 's'} found.",
        f"/compliance/scanner?script={row['id']}",
        row["id"],
    )


async def submit(submission_id: UUID, user_id: UUID) -> ScriptStage:
    """Send the uploaded script file for review (its text as read)."""
    row = await subs._get_owned_row(submission_id, user_id)
    subs._require_editable(row)
    _require_stage(row)
    if _status(row) not in ("DRAFT", "CHANGES_REQUESTED"):
        raise HTTPException(status_code=409, detail="The script is already with a reviewer or approved")
    file = await _script_file(submission_id)
    if not file:
        raise HTTPException(status_code=422, detail="Upload the script first (PDF, Word or text)")
    if file["doc_text_status"] in (None, "PENDING", "PROCESSING"):
        await document_text_service.read_file(file["id"])
        for _ in range(30):
            file = await _script_file(submission_id)
            if not file or file["doc_text_status"] not in (None, "PENDING", "PROCESSING"):
                break
            await asyncio.sleep(0.5)
    if not file or file["doc_text_status"] in (None, "PENDING", "PROCESSING"):
        raise HTTPException(status_code=409, detail="The script is still being read. Try again in a moment.")
    if file["doc_text_status"] != "COMPLETED" or not (file["doc_text"] or "").strip():
        raise HTTPException(status_code=422, detail=file["doc_text_error"] or "The script's text couldn't be read")
    body = _clean(file["doc_text"])
    previous = await _versions(submission_id)
    if previous and _status(row) == "CHANGES_REQUESTED" and _same(previous[-1].body, body):
        raise HTTPException(status_code=422, detail="This is the same script the reviewer sent back. Make the changes first.")
    return await _send(row, body, "UPLOAD", user_id, file_id=file["id"])


async def _send(row: Dict[str, Any], body: str, source: str, user_id: UUID, file_id: Optional[UUID] = None,
                note: Optional[str] = None) -> ScriptStage:
    check = await check_text(row, body)
    pool = get_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            await _lock_status(conn, row["id"], ("DRAFT",) + CREATOR_TURN)
            version = await _add_version(conn, row, body, source, user_id, check, file_id)
            await conn.execute(
                "UPDATE content_submissions SET script_status = 'IN_REVIEW', updated_at = NOW(), updated_by = $2 WHERE id = $1",
                row["id"], user_id,
            )
            await subs._add_event(
                conn, row["id"], "SCRIPT_SUBMITTED",
                f"Script version {version} sent for review (score {check.get('score')}/100)"
                + (f": {note.strip()}" if note and note.strip() else ""),
                user_id, {"script_version": version, "score": check.get("score")},
            )
    await log_audit_event("content_submission", str(row["id"]), "SUBMIT_SCRIPT", user_id,
                          {"reference_code": row["reference_code"], "script_version": version})
    await _send_to_reviewers(row, version, check, user_id)
    return await get_stage(row["id"], user_id)


async def decide(submission_id: UUID, user_id: UUID, req: ScriptDecisionRequest) -> ScriptStage:
    """The reviewer approves the script, sends it back with a note, or sends back their own edited version."""
    row = await subs._get_owned_row(submission_id, user_id, manager_can_read=True)
    _require_stage(row)
    if _status(row) != "IN_REVIEW":
        raise HTTPException(status_code=409, detail="This script isn't waiting for review")
    if not await _reviewer_may_act(row, user_id):
        raise HTTPException(status_code=403, detail="You can't review this script")
    versions = await _versions(submission_id)
    if not versions:
        raise HTTPException(status_code=409, detail="There's no script to review")
    sent = versions[-1]
    edited = _clean(req.text) if req.text is not None else None
    has_edits = bool(edited) and not _same(edited, sent.body)
    note = (req.note or "").strip() or None
    if req.text is not None and not edited:
        raise HTTPException(status_code=422, detail="The script can't be empty")
    if req.decision == "APPROVE" and has_edits:
        raise HTTPException(status_code=422,
                            detail="You changed the script, so it goes back to the creator to accept. Send it back instead.")
    if req.decision == "SEND_BACK" and not has_edits and not note:
        raise HTTPException(status_code=422, detail="Edit the script or say what needs to change")

    check = await check_text(row, edited) if has_edits else None
    if req.decision == "APPROVE":
        decision, new_status, message = "APPROVED", "APPROVED", f"Script version {sent.version_number} approved"
    elif has_edits:
        decision, new_status, message = "EDITED", "EDITS_PROPOSED", f"Reviewer edited script version {sent.version_number}"
    else:
        decision, new_status, message = ("CHANGES_REQUESTED", "CHANGES_REQUESTED",
                                         f"Changes requested on script version {sent.version_number}")
    pool = get_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            await _lock_status(conn, submission_id, ("IN_REVIEW",))
            await conn.execute(
                """
                UPDATE content_script_versions
                SET review_decision = $3, review_note = $4, reviewed_by = $5, reviewed_at = NOW()
                WHERE submission_id = $1 AND version_number = $2
                """,
                submission_id, sent.version_number, decision, note, user_id,
            )
            new_version = None
            if has_edits:
                new_version = await _add_version(conn, row, edited, "REVIEWER_EDIT", user_id, check)
            await conn.execute(
                "UPDATE content_submissions SET script_status = $2, updated_at = NOW() WHERE id = $1",
                submission_id, new_status,
            )
            await subs._add_event(conn, submission_id, f"SCRIPT_{decision}", message + (f": {note}" if note else ""),
                                  user_id, {"script_version": sent.version_number, "new_version": new_version})
    await log_audit_event("content_submission", str(submission_id), f"SCRIPT_{decision}", user_id,
                          {"reference_code": row["reference_code"], "script_version": sent.version_number})
    texts = {
        "APPROVED": ("Script approved", "is approved. You can upload the video now."),
        "EDITED": ("Script edited by your reviewer", "came back with edits. Compare the versions and accept or edit them."),
        "CHANGES_REQUESTED": ("Script needs changes", "needs changes before filming. See the reviewer's note."),
    }[decision]
    if row["created_by"] and row["created_by"] != user_id:
        await subs._notify([row["created_by"]], f"{texts[0]}: {row['reference_code']}", f"\"{row['title']}\" {texts[1]}",
                           f"/submissions/{submission_id}/script", submission_id)
    return await get_stage(submission_id, user_id)


async def accept(submission_id: UUID, user_id: UUID) -> ScriptStage:
    """The creator accepts the reviewer's edited version: it's the approved script."""
    row = await subs._get_owned_row(submission_id, user_id)
    subs._require_editable(row)
    _require_stage(row)
    if _status(row) != "EDITS_PROPOSED":
        raise HTTPException(status_code=409, detail="There are no reviewer edits to accept")
    pool = get_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            await _lock_status(conn, submission_id, ("EDITS_PROPOSED",))
            await conn.execute(
                "UPDATE content_submissions SET script_status = 'APPROVED', updated_at = NOW(), updated_by = $2 WHERE id = $1",
                submission_id, user_id,
            )
            await subs._add_event(conn, submission_id, "SCRIPT_EDITS_ACCEPTED",
                                  "Reviewer's edits accepted; script approved", user_id)
    await log_audit_event("content_submission", str(submission_id), "SCRIPT_EDITS_ACCEPTED", user_id,
                          {"reference_code": row["reference_code"]})
    last_reviewer = await fetch_one(
        """
        SELECT created_by FROM content_script_versions
        WHERE submission_id = $1 AND source = 'REVIEWER_EDIT' ORDER BY version_number DESC LIMIT 1
        """,
        submission_id,
    )
    if last_reviewer and last_reviewer["created_by"] and last_reviewer["created_by"] != user_id:
        await subs._notify([last_reviewer["created_by"]], f"Script edits accepted: {row['reference_code']}",
                           f"The creator accepted your edits to \"{row['title']}\". The script is approved.",
                           f"/compliance/scanner?id={submission_id}", submission_id)
    return await get_stage(submission_id, user_id)


async def revise(submission_id: UUID, user_id: UUID, req: ScriptReviseRequest) -> ScriptStage:
    """The creator edits the script in the platform (after edits or a change request) and sends it back."""
    row = await subs._get_owned_row(submission_id, user_id)
    subs._require_editable(row)
    _require_stage(row)
    if _status(row) not in CREATOR_TURN:
        raise HTTPException(status_code=409, detail="The script isn't waiting on you")
    versions = await _versions(submission_id)
    body = _clean(req.text)
    if versions and _same(versions[-1].body, body):
        detail = ("That's the reviewer's version unchanged. Accept it instead."
                  if _status(row) == "EDITS_PROPOSED" else "Make the changes the reviewer asked for first.")
        raise HTTPException(status_code=422, detail=detail)
    return await _send(row, body, "CREATOR_EDIT", user_id, note=req.note)


async def preview_check(submission_id: UUID, user_id: UUID, text: str) -> Dict[str, Any]:
    """Check wording being edited without saving it, so the score updates as the reviewer or creator works."""
    row = await subs._get_owned_row(submission_id, user_id, manager_can_read=True)
    _require_stage(row)
    return await check_text(row, _clean(text))


# Which scripts each Content Screener tab lists, as SQL on content_submissions (s) and its latest script version (v):
# waiting for a reviewer (oldest first); sent back to the creator; approved with the video still to come (the post
# itself takes over in the queue once the video is submitted), both over the last 30 days, newest first.
_QUEUE_TABS = {
    "review": ("s.script_status = 'IN_REVIEW'", "v.created_at"),
    "sent_back": ("s.script_status IN ('CHANGES_REQUESTED', 'EDITS_PROPOSED')", "s.updated_at DESC"),
    "done": ("s.script_status = 'APPROVED' AND s.status = 'DRAFT' AND s.updated_at > NOW() - INTERVAL '30 days'",
             "s.updated_at DESC"),
}


async def queue(user_id: UUID, tab: str = "review") -> List[ScriptQueueItem]:
    """Scripts for one Content Screener tab, on the brands the viewer works on."""
    if tab not in _QUEUE_TABS:
        raise HTTPException(status_code=422, detail="Unknown queue tab")
    if not await subs._can_see_team_content(user_id):
        return []
    is_super = await subs._is_super_admin(user_id)
    is_reviewer = await subs._can_review(user_id)
    where, order = _QUEUE_TABS[tab]
    rows = await fetch_all(
        f"""
        SELECT s.id, s.reference_code, s.title, s.content_type, s.platforms, s.brand_code, s.audience, s.region,
               s.rule_sets, s.created_by, s.approval_needed_by, s.script_status, s.updated_at, u.full_name AS creator_name,
               v.version_number, v.created_at AS sent_at, v.body AS script_body, v.check_result, v.reviewed_at
        FROM content_submissions s
        JOIN LATERAL (
            SELECT version_number, created_at, body, check_result, reviewed_at FROM content_script_versions sv
            WHERE sv.submission_id = s.id ORDER BY version_number DESC LIMIT 1
        ) v ON true
        LEFT JOIN users u ON u.id = s.created_by
        WHERE {where} AND s.script_required AND s.content_type = 'VIDEO'
          AND s.deleted_at IS NULL AND ($1::text[] IS NULL OR s.brand_code = ANY($1::text[]))
        ORDER BY {order}
        LIMIT 100
        """,
        await subs._brand_scope(user_id),
    )
    current_rb = await rulebook_version() if tab in ("review", "sent_back") else None
    items = []
    for r in rows:
        check = subs._json(r["check_result"]) or {}
        if current_rb and check.get("rulebook_version") != current_rb and r.get("script_body"):
            try:
                check = await check_text(r, r["script_body"])
                await execute(
                    "UPDATE content_script_versions SET check_result = $3::jsonb WHERE submission_id = $1 AND version_number = $2",
                    r["id"], r["version_number"], json.dumps(check),
                )
            except Exception as e:
                logger.warning("Failed to re-check script version on the fly: %s", e)
        items.append(ScriptQueueItem(
            id=r["id"], reference_code=r["reference_code"], title=r["title"], platforms=list(r["platforms"] or []),
            brand_code=r["brand_code"], audience=r["audience"], creator_name=r["creator_name"],
            created_by=r["created_by"], version_number=r["version_number"], sent_at=r["sent_at"],
            score=check.get("score"), traffic_light=check.get("traffic_light"),
            approval_needed_by=r["approval_needed_by"], script_status=r["script_status"],
            decided_at=r["reviewed_at"] or (r["updated_at"] if r["script_status"] != "IN_REVIEW" else None),
            can_review=r["script_status"] == "IN_REVIEW" and is_reviewer and (r["created_by"] != user_id or is_super),
        ))
    return items
