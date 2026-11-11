"""Script batches.

A creator uploads one document holding several scripts. It is split into scripts (see script_split), the creator sets the
details every post will share and sends the batch. Each script is checked against the rulebook, and a reviewer reviews
them all on one page, approving or sending back each. An approved script becomes a post of its own (a video post whose
script is already approved), so the creator can go on to film and upload the video.

    DRAFT -> PROCESSING -> IN_REVIEW -> DONE
                              |
                              +-> CHANGES_REQUESTED (some sent back) -> creator edits those -> PROCESSING -> IN_REVIEW
"""

import asyncio
import json
import logging
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional
from uuid import UUID

from fastapi import HTTPException

from models.compliance.script_batch_model import (
    Batch,
    BatchDetails,
    BatchItem,
    BatchItemCreate,
    BatchItemUpdate,
    BatchReviewRequest,
    BatchReviewResult,
    BatchSummary,
    BatchUpdate,
    ItemResult,
)
from models.compliance.submission_model import SubmissionCreate, SubmissionUpdate
from postgresql_db.database import execute, fetch_all, fetch_one, get_pool
from services.compliance.evaluation import script_review_service as scripts
from services.compliance.evaluation.compliance_service import rulebook_version
from services.compliance.evaluation.script_split import split_scripts
from services.compliance.media import document_text_service as documents
from services.compliance.submissions import review_deadline_service
from services.compliance.submissions import submission_service as subs
from services.compliance.submissions.flag_review_service import MIN_APPROVAL_SCORE
from services.shared.core.audit_service import log_audit_event

logger = logging.getLogger(__name__)

MAX_UPLOAD_BYTES = 10 * 1024 * 1024
MAX_SCRIPTS = 50
ACCEPTED = (".pdf", ".docx", ".doc", ".txt", ".md")
# How many scripts are checked at once, and how long a send may run before it counts as failed (a restart can cut it off).
CHECK_CONCURRENCY = 3
PROCESSING_TIMEOUT_SEC = 900
AI_RECHECK_AFTER_SEC = 120
_background: set = set()

# The details every post from a batch shares; the batch can't be sent until they are all filled in.
_DETAIL_LABELS = {
    "brand_codes": "Brand",
    "audiences": "Target audience",
    "region": "Region",
    "platforms": "Platform(s)",
    "account_ids": "Publishing account(s)",
    "rule_sets": "Rule sets",
    "approval_needed_by": "Need approval by",
    "reviewer_ids": "Send to reviewers",
}
_CREATOR_TURN = ("DRAFT", "CHANGES_REQUESTED")


def missing_details(details: Dict[str, Any]) -> List[str]:
    """The details still empty, by their names on the form."""
    return [label for field, label in _DETAIL_LABELS.items() if not details.get(field)]


def _json(value: Any) -> Any:
    return json.loads(value) if isinstance(value, str) else value


def _details(row: Dict[str, Any]) -> Dict[str, Any]:
    return _json(row["details"]) or {}


def post_row(batch: Dict[str, Any], title: str) -> Dict[str, Any]:
    """The batch's details shaped like a post row, so a script is checked against the rules scoped to its posts."""
    d = _details(batch)
    brands, audiences = list(d.get("brand_codes") or []), list(d.get("audiences") or [])
    return {
        "title": title, "content_type": "VIDEO", "script_required": True,
        "brand_code": brands[0] if brands else None, "brand_codes": brands,
        "audience": audiences[0] if audiences else None, "audiences": audiences,
        "region": d.get("region"), "platforms": list(d.get("platforms") or []), "rule_sets": list(d.get("rule_sets") or []),
        "reviewer_ids": [UUID(str(r)) for r in d.get("reviewer_ids") or []],
    }


# --- Reading the upload ------------------------------------------------------------------------------------------------

async def read_upload(filename: str, data: bytes) -> str:
    """The text of an uploaded document (PDF, Word or text), or a message the creator can act on."""
    ext = Path(filename or "").suffix.lower()
    if ext not in ACCEPTED:
        raise HTTPException(status_code=422, detail="Upload a PDF, Word (.docx) or text file")
    if not data:
        raise HTTPException(status_code=422, detail="That file is empty")
    if len(data) > MAX_UPLOAD_BYTES:
        raise HTTPException(status_code=413, detail="That file is too large (10 MB at most)")
    with tempfile.TemporaryDirectory() as folder:
        path = Path(folder) / f"upload{ext}"
        path.write_bytes(data)
        try:
            text = documents.read_local(path)
            if text is None:
                text = await documents._read_pdf(path)
        except documents.UnreadableDocument as e:
            raise HTTPException(status_code=422, detail=str(e))
        except HTTPException:
            raise
        except Exception:
            logger.exception("Couldn't read an uploaded script document")
            raise HTTPException(status_code=422, detail="That file couldn't be read. Try saving it as .docx or text.")
    if not (text or "").strip():
        raise HTTPException(status_code=422, detail="No text was found in that file")
    return text


# --- Loading and permissions -------------------------------------------------------------------------------------------

async def _load(batch_id: UUID) -> Dict[str, Any]:
    row = await fetch_one("SELECT * FROM content_script_batches WHERE id = $1 AND deleted_at IS NULL", batch_id)
    if not row:
        raise HTTPException(status_code=404, detail="Script batch not found")
    row = dict(row)
    # A send the server never finished (it restarted) counts as failed, so the creator can send again.
    if row["status"] == "PROCESSING" and (datetime.now(timezone.utc) - row["updated_at"]).total_seconds() > PROCESSING_TIMEOUT_SEC:
        await execute(
            "UPDATE content_script_batches SET status = 'DRAFT', error = $2, updated_at = NOW() WHERE id = $1",
            batch_id, "Sending took too long and was stopped. Send it again.",
        )
        row = dict(await fetch_one("SELECT * FROM content_script_batches WHERE id = $1", batch_id))
    return row


async def _reviewer_may_see(row: Dict[str, Any], user_id: UUID) -> bool:
    if row["status"] in ("DRAFT", "PROCESSING"):
        return False
    if not await subs._can_review(user_id):
        return False
    picked = [str(r) for r in _details(row).get("reviewer_ids") or []]
    return not picked or str(user_id) in picked or await subs._is_super_admin(user_id)


async def _may_see(row: Dict[str, Any], user_id: UUID) -> bool:
    return row["created_by"] == user_id or await subs._is_super_admin(user_id) or await _reviewer_may_see(row, user_id)


async def _owned(batch_id: UUID, user_id: UUID) -> Dict[str, Any]:
    row = await _load(batch_id)
    if row["created_by"] != user_id and not await subs._is_super_admin(user_id):
        raise HTTPException(status_code=403, detail="Only the person who uploaded these scripts can change them")
    return row


async def _reviewer_may_act(row: Dict[str, Any], user_id: UUID) -> bool:
    if not await _reviewer_may_see(row, user_id) and not await subs._is_super_admin(user_id):
        return False
    return row["created_by"] != user_id or await subs._is_super_admin(user_id)


# --- Reading a batch ---------------------------------------------------------------------------------------------------

_ITEMS_SQL = """
    SELECT i.*, u.full_name AS reviewed_by_name, s.reference_code
    FROM content_script_batch_items i
    LEFT JOIN users u ON u.id = i.reviewed_by
    LEFT JOIN content_submissions s ON s.id = i.submission_id
    WHERE i.batch_id = $1
    ORDER BY i.position
"""


def _item_out(r: Dict[str, Any]) -> BatchItem:
    return BatchItem(
        id=r["id"], position=r["position"], title=r["title"], body=r["body"], status=r["status"],
        check=_json(r["check_result"]), review_note=r["review_note"], reviewed_by_name=r["reviewed_by_name"],
        reviewed_at=r["reviewed_at"], submission_id=r["submission_id"], reference_code=r["reference_code"],
    )


async def _out(row: Dict[str, Any], user_id: UUID) -> Batch:
    items = [_item_out(dict(r)) for r in await fetch_all(_ITEMS_SQL, row["id"])]
    creator = await fetch_one("SELECT full_name FROM users WHERE id = $1", row["created_by"])
    is_creator = row["created_by"] == user_id or await subs._is_super_admin(user_id)
    details = _details(row)
    missing = missing_details(details)
    waiting = [i for i in items if i.status in _CREATOR_TURN]
    return Batch(
        id=row["id"], title=row["title"], source_name=row["source_name"], status=row["status"],
        created_by=row["created_by"], created_by_name=creator["full_name"] if creator else None,
        details=details, missing_details=missing, items=items, error=row["error"], sent_at=row["sent_at"],
        created_at=row["created_at"],
        can_edit=is_creator and row["status"] in _CREATOR_TURN,
        can_send=is_creator and row["status"] in _CREATOR_TURN and bool(waiting) and not missing,
        can_review=row["status"] == "IN_REVIEW" and await _reviewer_may_act(row, user_id),
    )


async def get_batch(batch_id: UUID, user_id: UUID) -> Batch:
    row = await _load(batch_id)
    if not await _may_see(row, user_id):
        raise HTTPException(status_code=404, detail="Script batch not found")
    if row["status"] == "IN_REVIEW":
        await _refresh_checks(row)
    return await _out(row, user_id)


async def list_batches(user_id: UUID, scope: str = "mine") -> List[BatchSummary]:
    """Batches the user uploaded ("mine"), or those waiting for or done by reviewers ("review")."""
    if scope == "review":
        if not await subs._can_review(user_id):
            raise HTTPException(status_code=403, detail="Only reviewers can see batches to review")
        rows = await fetch_all(
            "SELECT * FROM content_script_batches WHERE deleted_at IS NULL AND status IN ('IN_REVIEW', 'CHANGES_REQUESTED', 'DONE') "
            "ORDER BY (status = 'IN_REVIEW') DESC, coalesce(sent_at, created_at) DESC LIMIT 200"
        )
        rows = [r for r in rows if await _reviewer_may_see(dict(r), user_id)]
    else:
        rows = await fetch_all(
            "SELECT * FROM content_script_batches WHERE deleted_at IS NULL AND created_by = $1 ORDER BY created_at DESC LIMIT 200",
            user_id,
        )
    counts = await fetch_all(
        "SELECT batch_id, status, count(*) AS n FROM content_script_batch_items WHERE batch_id = ANY($1::uuid[]) GROUP BY 1, 2",
        [r["id"] for r in rows],
    )
    by_batch: Dict[UUID, Dict[str, int]] = {}
    for c in counts:
        by_batch.setdefault(c["batch_id"], {})[c["status"]] = c["n"]
    names = {u["id"]: u["full_name"] for u in await fetch_all(
        "SELECT id, full_name FROM users WHERE id = ANY($1::uuid[])", list({r["created_by"] for r in rows}))}
    return [
        BatchSummary(
            id=r["id"], title=r["title"], status=r["status"], created_by=r["created_by"],
            created_by_name=names.get(r["created_by"]), total=sum(by_batch.get(r["id"], {}).values()),
            in_review=by_batch.get(r["id"], {}).get("IN_REVIEW", 0), approved=by_batch.get(r["id"], {}).get("APPROVED", 0),
            changes_requested=by_batch.get(r["id"], {}).get("CHANGES_REQUESTED", 0),
            sent_at=r["sent_at"], created_at=r["created_at"],
        )
        for r in rows
    ]


# --- The creator's side ------------------------------------------------------------------------------------------------

async def create_batch(user_id: UUID, filename: str, data: bytes) -> Batch:
    """Split an uploaded document into scripts and start a batch of them (a draft the creator can still change)."""
    text = await read_upload(filename, data)
    found = split_scripts(text)
    if not found:
        raise HTTPException(status_code=422, detail="No scripts were found in that file")
    if len(found) > MAX_SCRIPTS:
        raise HTTPException(status_code=422, detail=f"That file has {len(found)} scripts; {MAX_SCRIPTS} is the most for one batch")
    title = (Path(filename).stem or "Scripts").strip()[:300]
    if len(found) == 1:
        found = [(title, found[0][1])]
    pool = get_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            batch_id = await conn.fetchval(
                "INSERT INTO content_script_batches (title, source_name, created_by) VALUES ($1, $2, $3) RETURNING id",
                title, (filename or "")[:300], user_id,
            )
            for n, (script_title, body) in enumerate(found, start=1):
                await conn.execute(
                    "INSERT INTO content_script_batch_items (batch_id, position, title, body) VALUES ($1, $2, $3, $4)",
                    batch_id, n, script_title[:300], body,
                )
    await log_audit_event("script_batch", str(batch_id), "CREATE_SCRIPT_BATCH", user_id,
                          {"file": filename, "scripts": len(found)})
    return await get_batch(batch_id, user_id)


async def update_batch(batch_id: UUID, user_id: UUID, req: BatchUpdate) -> Batch:
    row = await _owned(batch_id, user_id)
    if row["status"] not in _CREATOR_TURN:
        raise HTTPException(status_code=409, detail="The batch is with a reviewer, so its details can't change now")
    if req.title is not None:
        await execute("UPDATE content_script_batches SET title = $2, updated_at = NOW() WHERE id = $1", batch_id, req.title.strip())
    if req.details is not None:
        merged = {**_details(row), **json.loads(req.details.model_dump_json(exclude_unset=True))}
        await execute(
            "UPDATE content_script_batches SET details = $2::jsonb, updated_at = NOW() WHERE id = $1", batch_id, json.dumps(merged)
        )
    return await get_batch(batch_id, user_id)


async def _editable_item(batch_id: UUID, item_id: UUID, user_id: UUID) -> Dict[str, Any]:
    row = await _owned(batch_id, user_id)
    if row["status"] not in _CREATOR_TURN:
        raise HTTPException(status_code=409, detail="The batch is with a reviewer, so its scripts can't change now")
    item = await fetch_one("SELECT * FROM content_script_batch_items WHERE id = $1 AND batch_id = $2", item_id, batch_id)
    if not item:
        raise HTTPException(status_code=404, detail="Script not found")
    if item["status"] not in _CREATOR_TURN:
        raise HTTPException(status_code=409, detail="That script is already with a reviewer or approved")
    return dict(item)


async def add_item(batch_id: UUID, user_id: UUID, req: BatchItemCreate) -> Batch:
    row = await _owned(batch_id, user_id)
    if row["status"] not in _CREATOR_TURN:
        raise HTTPException(status_code=409, detail="The batch is with a reviewer, so scripts can't be added now")
    count = await fetch_one("SELECT count(*) AS n, coalesce(max(position), 0) AS last FROM content_script_batch_items WHERE batch_id = $1", batch_id)
    if count["n"] >= MAX_SCRIPTS:
        raise HTTPException(status_code=422, detail=f"A batch holds at most {MAX_SCRIPTS} scripts")
    await execute(
        "INSERT INTO content_script_batch_items (batch_id, position, title, body) VALUES ($1, $2, $3, $4)",
        batch_id, count["last"] + 1, req.title.strip(), req.body,
    )
    return await get_batch(batch_id, user_id)


async def update_item(batch_id: UUID, item_id: UUID, user_id: UUID, req: BatchItemUpdate) -> Batch:
    item = await _editable_item(batch_id, item_id, user_id)
    title = req.title.strip() if req.title is not None else item["title"]
    body = req.body if req.body is not None else item["body"]
    # New wording is checked again when the batch is sent.
    stale = ", check_result = NULL" if body != item["body"] or title != item["title"] else ""
    await execute(
        f"UPDATE content_script_batch_items SET title = $2, body = $3, updated_at = NOW(){stale} WHERE id = $1",
        item_id, title, body,
    )
    return await get_batch(batch_id, user_id)


async def delete_item(batch_id: UUID, item_id: UUID, user_id: UUID) -> Batch:
    await _editable_item(batch_id, item_id, user_id)
    await execute("DELETE FROM content_script_batch_items WHERE id = $1", item_id)
    return await get_batch(batch_id, user_id)


async def delete_batch(batch_id: UUID, user_id: UUID) -> None:
    """Remove a batch that hasn't produced any posts (nothing has been approved from it)."""
    await _owned(batch_id, user_id)
    made = await fetch_one(
        "SELECT 1 AS x FROM content_script_batch_items WHERE batch_id = $1 AND submission_id IS NOT NULL LIMIT 1", batch_id
    )
    if made:
        raise HTTPException(status_code=409, detail="Posts were already made from this batch, so it can't be deleted")
    await execute("UPDATE content_script_batches SET deleted_at = NOW() WHERE id = $1", batch_id)
    await log_audit_event("script_batch", str(batch_id), "DELETE_SCRIPT_BATCH", user_id, {})


async def send(batch_id: UUID, user_id: UUID) -> Batch:
    """Send the scripts that are waiting (new ones, and ones sent back) to the reviewers. They are checked against the
    rulebook in the background, so a long batch doesn't keep the creator waiting; the batch shows as processing until then."""
    row = await _owned(batch_id, user_id)
    if row["status"] not in _CREATOR_TURN:
        raise HTTPException(status_code=409, detail="This batch is already with a reviewer, or being sent")
    missing = missing_details(_details(row))
    if missing:
        raise HTTPException(status_code=422, detail=f"Fill in the details first: {', '.join(missing)}.")
    waiting = await fetch_all(
        "SELECT id, title, body FROM content_script_batch_items WHERE batch_id = $1 AND status IN ('DRAFT', 'CHANGES_REQUESTED')",
        batch_id,
    )
    if not waiting:
        raise HTTPException(status_code=422, detail="There are no scripts to send")
    empty = [w["title"] for w in waiting if not (w["body"] or "").strip()]
    if empty:
        raise HTTPException(status_code=422, detail=f"These scripts are empty: {', '.join(empty[:5])}")
    await execute("UPDATE content_script_batches SET status = 'PROCESSING', error = NULL, updated_at = NOW() WHERE id = $1", batch_id)
    task = asyncio.create_task(_process_send(batch_id, user_id))
    _background.add(task)
    task.add_done_callback(_background.discard)
    return await get_batch(batch_id, user_id)


async def _check_item(batch: Dict[str, Any], item: Dict[str, Any], gate: asyncio.Semaphore) -> Dict[str, Any]:
    async with gate:
        return await scripts.check_text(post_row(batch, item["title"]), item["body"])


async def _process_send(batch_id: UUID, user_id: UUID) -> None:
    """Check each waiting script, then hand the batch to the reviewers (or say what went wrong)."""
    try:
        batch = dict(await fetch_one("SELECT * FROM content_script_batches WHERE id = $1", batch_id))
        waiting = [dict(r) for r in await fetch_all(
            "SELECT * FROM content_script_batch_items WHERE batch_id = $1 AND status IN ('DRAFT', 'CHANGES_REQUESTED') ORDER BY position",
            batch_id,
        )]
        gate = asyncio.Semaphore(CHECK_CONCURRENCY)
        checks = await asyncio.gather(*(_check_item(batch, w, gate) for w in waiting))
        pool = get_pool()
        async with pool.acquire() as conn:
            async with conn.transaction():
                for item, check in zip(waiting, checks):
                    await conn.execute(
                        "UPDATE content_script_batch_items SET status = 'IN_REVIEW', check_result = $2::jsonb, review_note = NULL, "
                        "reviewed_by = NULL, reviewed_at = NULL, updated_at = NOW() WHERE id = $1",
                        item["id"], json.dumps(check),
                    )
                await conn.execute(
                    "UPDATE content_script_batches SET status = 'IN_REVIEW', error = NULL, sent_at = coalesce(sent_at, NOW()), updated_at = NOW() WHERE id = $1",
                    batch_id,
                )
        await _notify_reviewers(batch, len(waiting), user_id)
    except Exception as e:
        logger.exception("Couldn't send script batch %s", batch_id)
        await execute(
            "UPDATE content_script_batches SET status = 'DRAFT', error = $2, updated_at = NOW() WHERE id = $1",
            batch_id, f"The scripts couldn't be checked: {str(e)[:200] or e.__class__.__name__}. Send it again.",
        )


async def _notify_reviewers(batch: Dict[str, Any], count: int, creator: UUID) -> None:
    row = post_row(batch, batch["title"])
    reviewers = await subs._review_recipients(row, exclude=creator)
    await subs._notify(
        reviewers, f"Scripts to review: {batch['title']}",
        f"{count} script{'' if count == 1 else 's'} are ready for review together.",
        f"/script-batches/{batch['id']}", batch["id"],
    )


async def _refresh_checks(row: Dict[str, Any]) -> None:
    """Check again the waiting scripts whose check was made without the AI, or against an older rulebook, so a reviewer
    never decides on a score that is out of date."""
    current = await rulebook_version()
    stale = [dict(r) for r in await fetch_all(
        "SELECT * FROM content_script_batch_items WHERE batch_id = $1 AND status = 'IN_REVIEW'", row["id"],
    ) if _needs_recheck(_json(r["check_result"]), current, r["updated_at"])]
    if not stale:
        return
    gate = asyncio.Semaphore(CHECK_CONCURRENCY)
    for item, check in zip(stale[:10], await asyncio.gather(*(_check_item(row, s, gate) for s in stale[:10]))):
        await execute(
            "UPDATE content_script_batch_items SET check_result = $2::jsonb, updated_at = NOW() WHERE id = $1 AND status = 'IN_REVIEW'",
            item["id"], json.dumps(check),
        )


def _needs_recheck(check: Optional[Dict[str, Any]], current: Optional[str], saved_at: Optional[datetime] = None) -> bool:
    if not check:
        return True
    if check.get("ai_unavailable"):
        # Not on every page load while the AI is down: at most once every couple of minutes.
        return saved_at is None or (datetime.now(timezone.utc) - saved_at).total_seconds() > AI_RECHECK_AFTER_SEC
    return bool(current and check.get("rulebook_version") != current)


# --- The reviewer's side -----------------------------------------------------------------------------------------------

def approval_block(check: Optional[Dict[str, Any]]) -> Optional[str]:
    """Why a script can't be approved yet, or None. The same standing a post's script needs."""
    if not check:
        return "It has no compliance check yet"
    if check.get("ai_unavailable"):
        return "The AI review couldn't run on it, so it has no score yet (it is checked again automatically)"
    critical = [v for v in check.get("violations") or [] if v.get("traffic_light") == "RED"]
    if critical:
        return f"{len(critical)} critical compliance flag{'s' if len(critical) > 1 else ''}"
    if check.get("score") is not None and int(check["score"]) < MIN_APPROVAL_SCORE:
        return f"Its score is {check['score']}/100; approval needs at least {MIN_APPROVAL_SCORE}"
    return None


async def _make_post(batch: Dict[str, Any], item: Dict[str, Any], reviewer_id: UUID, due_at: Optional[datetime]) -> Dict[str, Any]:
    """The post for an approved script: a video post of the creator's, with the batch's details and the script already
    approved, so the next step is the video."""
    creator = batch["created_by"]
    post = await subs.create_submission(SubmissionCreate(title=item["title"], content_type="VIDEO"), creator)
    try:
        shared = BatchDetails(**_details(batch)).model_dump(exclude_none=True)
        await subs.update_submission(post.id, SubmissionUpdate(**shared), creator)
    except Exception:
        await execute("UPDATE content_submissions SET deleted_at = NOW() WHERE id = $1", post.id)
        raise
    check = _json(item["check_result"]) or {}
    pool = get_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            await conn.execute(
                """
                INSERT INTO content_script_versions (submission_id, version_number, body, source, check_result, created_by,
                                                     review_decision, reviewed_by, reviewed_at)
                VALUES ($1, 1, $2, 'UPLOAD', $3::jsonb, $4, 'APPROVED', $5, NOW())
                """,
                post.id, item["body"], json.dumps(check), creator, reviewer_id,
            )
            await scripts._adopt_check(conn.execute, post.id, check)
            await conn.execute(
                "UPDATE content_submissions SET script_status = 'APPROVED', due_at = $2, "
                "due_kind = CASE WHEN $2::timestamptz IS NULL THEN NULL ELSE 'VIDEO' END, updated_at = NOW() WHERE id = $1",
                post.id, due_at,
            )
            await subs._add_event(conn, post.id, "SCRIPT_APPROVED",
                                  f"Script approved in a batch review of \"{batch['title']}\"", reviewer_id,
                                  {"batch_id": str(batch["id"]), "script_version": 1})
            await conn.execute(
                "UPDATE content_script_batch_items SET status = 'APPROVED', submission_id = $2, reviewed_by = $3, "
                "reviewed_at = NOW(), review_note = NULL, updated_at = NOW() WHERE id = $1",
                item["id"], post.id, reviewer_id,
            )
    return {"id": post.id, "reference_code": post.reference_code}


async def review(batch_id: UUID, user_id: UUID, req: BatchReviewRequest) -> BatchReviewResult:
    """Decide on any number of the batch's scripts at once. Each is handled on its own: one that can't be approved yet
    is reported and left waiting, and the others still go through."""
    row = await _load(batch_id)
    if not await _may_see(row, user_id):
        raise HTTPException(status_code=404, detail="Script batch not found")
    if row["status"] != "IN_REVIEW":
        raise HTTPException(status_code=409, detail="This batch isn't waiting for review")
    if not await _reviewer_may_act(row, user_id):
        raise HTTPException(status_code=403, detail="You can't review this batch")
    due_at = review_deadline_service.video_due(None, req.due_days, req.due_at)
    items = {i["id"]: dict(i) for i in await fetch_all("SELECT * FROM content_script_batch_items WHERE batch_id = $1", batch_id)}
    results: List[ItemResult] = []
    seen: set = set()
    for d in req.decisions:
        item = items.get(d.item_id)
        if d.item_id in seen:
            continue
        seen.add(d.item_id)
        if not item:
            results.append(ItemResult(item_id=d.item_id, ok=False, outcome="That script isn't in this batch"))
            continue
        if item["status"] != "IN_REVIEW":
            results.append(ItemResult(item_id=d.item_id, ok=False, outcome="It isn't waiting for review"))
            continue
        if d.decision == "SEND_BACK":
            note = (d.note or "").strip()
            if not note:
                results.append(ItemResult(item_id=d.item_id, ok=False, outcome="Say what needs to change"))
                continue
            await execute(
                "UPDATE content_script_batch_items SET status = 'CHANGES_REQUESTED', review_note = $2, reviewed_by = $3, "
                "reviewed_at = NOW(), updated_at = NOW() WHERE id = $1 AND status = 'IN_REVIEW'",
                d.item_id, note, user_id,
            )
            results.append(ItemResult(item_id=d.item_id, ok=True, outcome="SENT_BACK"))
            continue
        block = approval_block(_json(item["check_result"]))
        if block:
            results.append(ItemResult(item_id=d.item_id, ok=False, outcome=block))
            continue
        try:
            post = await _make_post(row, item, user_id, due_at)
        except HTTPException as e:
            results.append(ItemResult(item_id=d.item_id, ok=False, outcome=f"The post couldn't be made: {e.detail}"))
            continue
        except Exception:
            logger.exception("Couldn't make a post from script %s", d.item_id)
            results.append(ItemResult(item_id=d.item_id, ok=False, outcome="The post couldn't be made"))
            continue
        results.append(ItemResult(item_id=d.item_id, ok=True, outcome="APPROVED",
                                  submission_id=post["id"], reference_code=post["reference_code"]))
    await _refresh_status(batch_id)
    approved = [r for r in results if r.outcome == "APPROVED"]
    sent_back = [r for r in results if r.outcome == "SENT_BACK"]
    if approved or sent_back:
        parts = []
        if approved:
            parts.append(f"{len(approved)} approved and made into posts for you to film")
        if sent_back:
            parts.append(f"{len(sent_back)} sent back with notes")
        await subs._notify([row["created_by"]], f"Your scripts were reviewed: {row['title']}", ", ".join(parts) + ".",
                           f"/script-batches/{batch_id}", batch_id)
        await log_audit_event("script_batch", str(batch_id), "REVIEW_SCRIPT_BATCH", user_id,
                              {"approved": len(approved), "sent_back": len(sent_back)})
    return BatchReviewResult(batch=await _out(await _load(batch_id), user_id), results=results)


async def _refresh_status(batch_id: UUID) -> None:
    """IN_REVIEW while any script waits for a reviewer, CHANGES_REQUESTED while only the creator has something to do,
    DONE once every script is approved."""
    counts = {r["status"]: r["n"] for r in await fetch_all(
        "SELECT status, count(*) AS n FROM content_script_batch_items WHERE batch_id = $1 GROUP BY 1", batch_id)}
    if counts.get("IN_REVIEW"):
        status = "IN_REVIEW"
    elif counts.get("CHANGES_REQUESTED") or counts.get("DRAFT"):
        status = "CHANGES_REQUESTED"
    else:
        status = "DONE"
    await execute("UPDATE content_script_batches SET status = $2, updated_at = NOW() WHERE id = $1", batch_id, status)
