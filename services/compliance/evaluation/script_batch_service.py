"""Script batches.

A creator uploads one document holding several scripts. It is split into scripts (see script_split), the creator sets the
details every post will share and sends the batch. Sending makes a post for each script and sends its script for review
exactly as a single script upload does, so each script is reviewed on its own page with everything that has: the
compliance flags, highlights and comments, the reviewer's own edits, the creator's replies. The batch ties them together:
one page to see where every script stands (and to approve or send back several at once), and previous / next
links on each script's page.

    batch DRAFT -> PROCESSING (posts made, scripts checked and sent) -> in review -> done
                   each script then follows the single-script flow: IN_REVIEW -> APPROVED, or sent back / edited and
                   answered by the creator, on its post.

A batch's own status is worked out from its scripts, so it is always what the posts say.
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
from pydantic import ValidationError

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
from models.compliance.script_review_model import ScriptDecisionRequest
from models.compliance.submission_model import SubmissionCreate, SubmissionUpdate
from postgresql_db.database import execute, fetch_all, fetch_one, get_pool
from services.compliance.evaluation import script_review_service as scripts
from services.compliance.evaluation.compliance_service import rulebook_version
from services.compliance.evaluation.script_split import split_scripts
from services.compliance.media import document_text_service as documents
from services.compliance.submissions import submission_service as subs
from services.shared.core.audit_service import log_audit_event

logger = logging.getLogger(__name__)

MAX_UPLOAD_BYTES = 10 * 1024 * 1024
MAX_SCRIPTS = 50
ACCEPTED = (".pdf", ".docx", ".doc", ".txt", ".md")
# How many scripts are checked at once, and how long a send may run before it counts as failed (a restart can cut it off).
CHECK_CONCURRENCY = 3
PROCESSING_TIMEOUT_SEC = 900
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


def missing_details(details: Dict[str, Any]) -> List[str]:
    """The details still empty, by their names on the form."""
    return [label for field, label in _DETAIL_LABELS.items() if not details.get(field)]


def _json(value: Any) -> Any:
    return json.loads(value) if isinstance(value, str) else value


def _details(row: Dict[str, Any]) -> Dict[str, Any]:
    return _json(row["details"]) or {}


def effective_details(batch: Dict[str, Any], item: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """The details a script's post gets: its own when the batch sets them per script, else the batch's."""
    if batch.get("per_script_details") and item is not None:
        return _json(item.get("details") or item.get("item_details")) or {}
    return _details(batch)


def post_row(d: Dict[str, Any], title: str) -> Dict[str, Any]:
    """A script's details shaped like a post row, so it is checked against the rules scoped to its post."""
    brands, audiences = list(d.get("brand_codes") or []), list(d.get("audiences") or [])
    return {
        "title": title, "content_type": "VIDEO", "script_required": True,
        "brand_code": brands[0] if brands else None, "brand_codes": brands,
        "audience": audiences[0] if audiences else None, "audiences": audiences,
        "region": d.get("region"), "platforms": list(d.get("platforms") or []), "rule_sets": list(d.get("rule_sets") or []),
        "reviewer_ids": [UUID(str(r)) for r in d.get("reviewer_ids") or []],
    }


# A script's status on its post, as the batch shows it.
_ITEM_STATUS = {"IN_REVIEW": "IN_REVIEW", "CHANGES_REQUESTED": "CHANGES_REQUESTED", "EDITS_PROPOSED": "CHANGES_REQUESTED",
                "APPROVED": "APPROVED"}


def item_status(has_post: bool, script_status: Optional[str]) -> str:
    """DRAFT until the batch is sent; then whatever its script is on its post. Edits the reviewer proposed wait for the
    creator, so they count as changes requested."""
    return _ITEM_STATUS.get(script_status or "", "DRAFT") if has_post else "DRAFT"


def batch_status(stored: str, statuses: List[str], has_posts: bool) -> str:
    """A batch is what its scripts are: in review while any waits for a reviewer, with the creator while any of the rest
    still needs them, done when every script is approved. Before it is first sent it is a draft."""
    if stored == "PROCESSING":
        return "PROCESSING"
    if not has_posts or not statuses:
        return "DRAFT"
    if "IN_REVIEW" in statuses:
        return "IN_REVIEW"
    return "DONE" if all(s == "APPROVED" for s in statuses) else "CHANGES_REQUESTED"


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


async def _picked_reviewers(row: Dict[str, Any]) -> Optional[set]:
    """The reviewers a batch was sent to, or None when anyone may review it (some script names no reviewer). With details
    per script it is everyone any of its scripts was sent to."""
    if not row.get("per_script_details"):
        picked = {str(r) for r in _details(row).get("reviewer_ids") or []}
        return picked or None
    found: set = set()
    for r in await fetch_all("SELECT details FROM content_script_batch_items WHERE batch_id = $1", row["id"]):
        picked = (_json(r["details"]) or {}).get("reviewer_ids") or []
        if not picked:
            return None
        found |= {str(p) for p in picked}
    return found or None


async def _reviewer_may_see(row: Dict[str, Any], user_id: UUID) -> bool:
    # Reviewers see a batch once it has been sent.
    if not row["sent_at"] or not await subs._can_suggest(user_id):
        return False
    picked = await _picked_reviewers(row)
    return picked is None or str(user_id) in picked or await subs._is_super_admin(user_id)


async def _may_see(row: Dict[str, Any], user_id: UUID) -> bool:
    return row["created_by"] == user_id or await subs._is_super_admin(user_id) or await _reviewer_may_see(row, user_id)


async def _owned(batch_id: UUID, user_id: UUID) -> Dict[str, Any]:
    row = await _load(batch_id)
    if row["created_by"] != user_id and not await subs._is_super_admin(user_id):
        raise HTTPException(status_code=403, detail="Only the person who uploaded these scripts can change them")
    return row


async def _reviewer_may_act(row: Dict[str, Any], user_id: UUID) -> bool:
    # Suggesting reviewers see a batch sent to them but can't decide on it.
    if not await subs._can_review(user_id):
        return False
    if not await _reviewer_may_see(row, user_id) and not await subs._is_super_admin(user_id):
        return False
    # Nobody approves their own scripts but a super admin (as with posts): another reviewer has to.
    return row["created_by"] != user_id or await subs._is_super_admin(user_id)


# --- Reading a batch ---------------------------------------------------------------------------------------------------

# Each script as its post has it now: the latest version of its script (the reviewer's edits included) and its decision.
_ITEMS_SQL = """
    SELECT i.id, i.position, i.title AS item_title, i.body AS item_body, i.submission_id, i.details AS item_details, i.precheck,
           s.reference_code, s.title AS post_title, s.script_status,
           v.body AS version_body, v.check_result, v.review_note, v.reviewed_at, ru.full_name AS reviewed_by_name
    FROM content_script_batch_items i
    LEFT JOIN content_submissions s ON s.id = i.submission_id AND s.deleted_at IS NULL
    LEFT JOIN LATERAL (
        SELECT * FROM content_script_versions WHERE submission_id = s.id ORDER BY version_number DESC LIMIT 1
    ) v ON true
    LEFT JOIN users ru ON ru.id = v.reviewed_by
    WHERE i.batch_id = $1 AND (i.submission_id IS NULL OR s.id IS NOT NULL)
    ORDER BY i.position
"""


def _item_out(r: Dict[str, Any], batch: Dict[str, Any], rulebook: Optional[str] = None) -> BatchItem:
    has_post = r["submission_id"] is not None
    details = effective_details(batch, r)
    per_script = bool(batch.get("per_script_details"))
    # Not sent yet: the creator's own check, if they ran one (stale once the script or its details changed).
    stored = _json(r.get("precheck")) or {}
    precheck = None if has_post else stored.get("check")
    stale = bool(precheck) and scripts.still_valid(stored, post_row(details, r["item_title"]), r["item_body"], rulebook) is None
    return BatchItem(
        details=details, missing_details=missing_details(details) if per_script else [],
        id=r["id"], position=r["position"], title=r["post_title"] if has_post else r["item_title"],
        body=(r["version_body"] if has_post and r["version_body"] is not None else r["item_body"]),
        status=item_status(has_post, r["script_status"]), check=_json(r["check_result"]) if has_post else precheck,
        check_stale=stale,
        review_note=r["review_note"] if has_post else None, reviewed_by_name=r["reviewed_by_name"] if has_post else None,
        reviewed_at=r["reviewed_at"] if has_post else None, submission_id=r["submission_id"], reference_code=r["reference_code"],
    )


async def _out(row: Dict[str, Any], user_id: UUID) -> Batch:
    rulebook = await rulebook_version()
    items = [_item_out(dict(r), row, rulebook) for r in await fetch_all(_ITEMS_SQL, row["id"])]
    creator = await fetch_one("SELECT full_name FROM users WHERE id = $1", row["created_by"])
    is_creator = row["created_by"] == user_id or await subs._is_super_admin(user_id)
    details = _details(row)
    per_script = bool(row["per_script_details"])
    missing = [] if per_script else missing_details(details)
    status = batch_status(row["status"], [i.status for i in items], any(i.submission_id for i in items))
    unsent = [i for i in items if i.status == "DRAFT"]
    return Batch(
        id=row["id"], title=row["title"], source_name=row["source_name"], status=status,
        created_by=row["created_by"], created_by_name=creator["full_name"] if creator else None,
        details=details, per_script_details=per_script, missing_details=missing, items=items, error=row["error"],
        sent_at=row["sent_at"],
        created_at=row["created_at"],
        can_edit=is_creator and row["sent_at"] is None and status != "PROCESSING",
        can_change_scripts=is_creator and status != "PROCESSING",
        can_send=is_creator and status != "PROCESSING" and bool(unsent) and not missing
        and not any(i.missing_details for i in unsent),
        can_review=bool(row["sent_at"]) and status == "IN_REVIEW" and await _reviewer_may_act(row, user_id),
    )


async def get_batch(batch_id: UUID, user_id: UUID) -> Batch:
    row = await _load(batch_id)
    if not await _may_see(row, user_id):
        raise HTTPException(status_code=404, detail="Script batch not found")
    return await _out(row, user_id)


async def list_batches(user_id: UUID, scope: str = "mine") -> List[BatchSummary]:
    """Batches the user uploaded ("mine"), or those sent to reviewers ("review")."""
    if scope == "review":
        if not await subs._can_suggest(user_id):
            raise HTTPException(status_code=403, detail="Only reviewers can see batches to review")
        rows = await fetch_all(
            "SELECT * FROM content_script_batches WHERE deleted_at IS NULL AND sent_at IS NOT NULL "
            "ORDER BY sent_at DESC LIMIT 200"
        )
        rows = [r for r in rows if await _reviewer_may_see(dict(r), user_id)]
    else:
        rows = await fetch_all(
            "SELECT * FROM content_script_batches WHERE deleted_at IS NULL AND created_by = $1 ORDER BY created_at DESC LIMIT 200",
            user_id,
        )
    counts = await fetch_all(
        """
        SELECT i.batch_id,
               CASE WHEN i.submission_id IS NULL THEN 'DRAFT'
                    WHEN s.script_status = 'IN_REVIEW' THEN 'IN_REVIEW'
                    WHEN s.script_status IN ('CHANGES_REQUESTED', 'EDITS_PROPOSED') THEN 'CHANGES_REQUESTED'
                    WHEN s.script_status = 'APPROVED' THEN 'APPROVED'
                    ELSE 'DRAFT' END AS st,
               (i.submission_id IS NOT NULL) AS has_post, count(*) AS n
        FROM content_script_batch_items i
        LEFT JOIN content_submissions s ON s.id = i.submission_id AND s.deleted_at IS NULL
        WHERE i.batch_id = ANY($1::uuid[]) AND (i.submission_id IS NULL OR s.id IS NOT NULL)
        GROUP BY 1, 2, 3
        """,
        [r["id"] for r in rows],
    )
    by_batch: Dict[UUID, Dict[str, int]] = {}
    posted: Dict[UUID, bool] = {}
    for c in counts:
        by_batch.setdefault(c["batch_id"], {})[c["st"]] = by_batch.get(c["batch_id"], {}).get(c["st"], 0) + c["n"]
        posted[c["batch_id"]] = posted.get(c["batch_id"], False) or c["has_post"]
    names = {u["id"]: u["full_name"] for u in await fetch_all(
        "SELECT id, full_name FROM users WHERE id = ANY($1::uuid[])", list({r["created_by"] for r in rows}))}
    out = []
    for r in rows:
        c = by_batch.get(r["id"], {})
        status = batch_status(r["status"], [s for s, n in c.items() for _ in range(n)], posted.get(r["id"], False))
        out.append(BatchSummary(
            id=r["id"], title=r["title"], status=status, created_by=r["created_by"],
            created_by_name=names.get(r["created_by"]), total=sum(c.values()), in_review=c.get("IN_REVIEW", 0),
            approved=c.get("APPROVED", 0), changes_requested=c.get("CHANGES_REQUESTED", 0),
            sent_at=r["sent_at"], created_at=r["created_at"],
        ))
    if scope == "review":
        out.sort(key=lambda b: (b.status != "IN_REVIEW", -(b.sent_at or b.created_at).timestamp()))
    return out


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
    if row["status"] == "PROCESSING":
        raise HTTPException(status_code=409, detail="The scripts are being sent, so the details can't change now")
    if req.details is not None and row["sent_at"]:
        raise HTTPException(status_code=409, detail="The scripts were already sent, and their posts keep the details they were sent with")
    if req.per_script_details is not None and req.per_script_details != bool(row["per_script_details"]):
        if row["sent_at"]:
            raise HTTPException(status_code=409, detail="The scripts were already sent, so how their details are set can't change now")
        if req.per_script_details:
            # Each script starts from the batch's details, then can be changed on its own.
            await execute(
                "UPDATE content_script_batch_items SET details = $2::jsonb WHERE batch_id = $1 AND details IS NULL",
                batch_id, json.dumps(_details(row)),
            )
        await execute(
            "UPDATE content_script_batches SET per_script_details = $2, updated_at = NOW() WHERE id = $1",
            batch_id, req.per_script_details,
        )
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
    if row["status"] == "PROCESSING":
        raise HTTPException(status_code=409, detail="The scripts are being sent, so they can't change now")
    item = await fetch_one("SELECT * FROM content_script_batch_items WHERE id = $1 AND batch_id = $2", item_id, batch_id)
    if not item:
        raise HTTPException(status_code=404, detail="Script not found")
    if item["submission_id"]:
        raise HTTPException(status_code=409, detail="That script was sent. Change it on its own page.")
    return dict(item)


async def add_item(batch_id: UUID, user_id: UUID, req: BatchItemCreate) -> Batch:
    row = await _owned(batch_id, user_id)
    if row["status"] == "PROCESSING":
        raise HTTPException(status_code=409, detail="The scripts are being sent, so scripts can't be added now")
    count = await fetch_one("SELECT count(*) AS n, coalesce(max(position), 0) AS last FROM content_script_batch_items WHERE batch_id = $1", batch_id)
    if count["n"] >= MAX_SCRIPTS:
        raise HTTPException(status_code=422, detail=f"A batch holds at most {MAX_SCRIPTS} scripts")
    start = json.dumps(_details(row)) if row["per_script_details"] else None
    await execute(
        "INSERT INTO content_script_batch_items (batch_id, position, title, body, details) VALUES ($1, $2, $3, $4, $5::jsonb)",
        batch_id, count["last"] + 1, req.title.strip(), req.body, start,
    )
    return await get_batch(batch_id, user_id)


async def update_item(batch_id: UUID, item_id: UUID, user_id: UUID, req: BatchItemUpdate) -> Batch:
    item = await _editable_item(batch_id, item_id, user_id)
    title = req.title.strip() if req.title is not None else item["title"]
    body = req.body if req.body is not None else item["body"]
    details = _json(item.get("details"))
    if req.details is not None:
        row = await _load(batch_id)
        if not row["per_script_details"]:
            raise HTTPException(status_code=409, detail="The details are set once for the whole batch. Switch to details per script first.")
        details = {**(details or {}), **json.loads(req.details.model_dump_json(exclude_unset=True))}
    await execute(
        "UPDATE content_script_batch_items SET title = $2, body = $3, details = $4::jsonb, updated_at = NOW() WHERE id = $1",
        item_id, title, body, json.dumps(details) if details is not None else None,
    )
    return await get_batch(batch_id, user_id)


async def delete_item(batch_id: UUID, item_id: UUID, user_id: UUID) -> Batch:
    await _editable_item(batch_id, item_id, user_id)
    await execute("DELETE FROM content_script_batch_items WHERE id = $1", item_id)
    return await get_batch(batch_id, user_id)


async def delete_batch(batch_id: UUID, user_id: UUID) -> None:
    """Remove a batch whose scripts haven't been sent (no posts were made from it)."""
    await _owned(batch_id, user_id)
    made = await fetch_one(
        "SELECT 1 AS x FROM content_script_batch_items WHERE batch_id = $1 AND submission_id IS NOT NULL LIMIT 1", batch_id
    )
    if made:
        raise HTTPException(status_code=409, detail="Posts were already made from this batch, so it can't be deleted")
    await execute("UPDATE content_script_batches SET deleted_at = NOW() WHERE id = $1", batch_id)
    await log_audit_event("script_batch", str(batch_id), "DELETE_SCRIPT_BATCH", user_id, {})


async def _unsent(batch_id: UUID) -> List[Dict[str, Any]]:
    """The scripts not sent yet: no post, or a post whose script was never sent for review."""
    rows = await fetch_all(
        """
        SELECT i.*, s.script_status FROM content_script_batch_items i
        LEFT JOIN content_submissions s ON s.id = i.submission_id AND s.deleted_at IS NULL
        WHERE i.batch_id = $1 AND (i.submission_id IS NULL OR s.id IS NOT NULL)
        ORDER BY i.position
        """,
        batch_id,
    )
    return [dict(r) for r in rows if item_status(r["submission_id"] is not None, r["script_status"]) == "DRAFT"]


async def send(batch_id: UUID, user_id: UUID) -> Batch:
    """Send the scripts that haven't been sent: a post is made for each and its script goes to the reviewers, as when a
    single script is sent. It happens in the background (each script is checked against the rulebook first), so a long
    batch doesn't keep the creator waiting; the batch shows as processing until then."""
    row = await _owned(batch_id, user_id)
    if row["status"] == "PROCESSING":
        raise HTTPException(status_code=409, detail="This batch is already being sent")
    waiting = await _unsent(batch_id)
    if not waiting:
        raise HTTPException(status_code=422, detail="There are no scripts to send")
    if row["per_script_details"]:
        gaps = [f"{w['title']} ({', '.join(missing_details(effective_details(row, w)))})"
                for w in waiting if missing_details(effective_details(row, w))]
        if gaps:
            raise HTTPException(status_code=422, detail=f"Fill in the details of: {'; '.join(gaps[:5])}.")
    else:
        missing = missing_details(_details(row))
        if missing:
            raise HTTPException(status_code=422, detail=f"Fill in the details first: {', '.join(missing)}.")
    empty = [w["title"] for w in waiting if not (w["body"] or "").strip()]
    if empty:
        raise HTTPException(status_code=422, detail=f"These scripts are empty: {', '.join(empty[:5])}")
    await execute("UPDATE content_script_batches SET status = 'PROCESSING', error = NULL, updated_at = NOW() WHERE id = $1", batch_id)
    task = asyncio.create_task(_process_send(batch_id, user_id))
    _background.add(task)
    task.add_done_callback(_background.discard)
    return await get_batch(batch_id, user_id)


async def _check_item(batch: Dict[str, Any], item: Dict[str, Any], gate: asyncio.Semaphore) -> Dict[str, Any]:
    post = post_row(effective_details(batch, item), item["title"])
    # The creator's own check of this same script is reused; otherwise it's checked now.
    reused = scripts.still_valid(item.get("precheck"), post, item["body"], await rulebook_version())
    if reused:
        return reused
    async with gate:
        return await scripts.check_text(post, item["body"])


async def check_item(batch_id: UUID, item_id: UUID, user_id: UUID) -> Batch:
    """Creator: check one script against the rulebook before sending it, to see its score and flags and fix them
    first. The result is kept and reused when the batch is sent with the script unchanged."""
    item = await _editable_item(batch_id, item_id, user_id)
    if not (item["body"] or "").strip():
        raise HTTPException(status_code=422, detail="This script is empty")
    batch = dict(await _load(batch_id))
    post = post_row(effective_details(batch, item), item["title"])
    check = await scripts.check_text(post, item["body"])
    await execute(
        "UPDATE content_script_batch_items SET precheck = $2::jsonb WHERE id = $1",
        item_id, scripts.precheck_record(post, item["body"], check),
    )
    return await get_batch(batch_id, user_id)


async def _make_post(batch: Dict[str, Any], item: Dict[str, Any]) -> UUID:
    """The post for a script: a video post of the creator's with the batch's details, linked to the script."""
    creator = batch["created_by"]
    post = await subs.create_submission(SubmissionCreate(title=item["title"], content_type="VIDEO"), creator)
    try:
        shared = BatchDetails(**effective_details(batch, item)).model_dump(exclude_none=True)
        await subs.update_submission(post.id, SubmissionUpdate(**shared), creator)
    except Exception:
        await execute("UPDATE content_submissions SET deleted_at = NOW() WHERE id = $1", post.id)
        raise
    await execute("UPDATE content_script_batch_items SET submission_id = $2, updated_at = NOW() WHERE id = $1", item["id"], post.id)
    return post.id


async def _process_send(batch_id: UUID, user_id: UUID) -> None:
    """Check each unsent script, make its post and send its script for review. A script that fails is reported and can be
    sent again; the others still go."""
    try:
        batch = dict(await fetch_one("SELECT * FROM content_script_batches WHERE id = $1", batch_id))
        waiting = await _unsent(batch_id)
        gate = asyncio.Semaphore(CHECK_CONCURRENCY)
        checks = await asyncio.gather(*(_check_item(batch, w, gate) for w in waiting), return_exceptions=True)
        sent, failed = 0, []
        for item, check in zip(waiting, checks):
            try:
                if isinstance(check, BaseException):
                    raise check
                post_id = item["submission_id"] or await _make_post(batch, item)
                post = await subs._get_owned_row(post_id, batch["created_by"])
                await scripts._send_checked(post, item["body"], "UPLOAD", batch["created_by"], check, notify=False)
                sent += 1
            except HTTPException as e:
                failed.append(f"{item['title']}: {e.detail}")
            except Exception as e:  # noqa: BLE001 - one script failing must not stop the rest
                logger.exception("Couldn't send script %s of batch %s", item["id"], batch_id)
                failed.append(f"{item['title']}: {str(e)[:120] or e.__class__.__name__}")
        error = None
        if failed:
            shown = "; ".join(failed[:3]) + (f" and {len(failed) - 3} more" if len(failed) > 3 else "")
            error = f"{len(failed)} script{'' if len(failed) == 1 else 's'} couldn't be sent: {shown}. Send again to retry them."
        await execute(
            "UPDATE content_script_batches SET status = 'DRAFT', error = $2, "
            "sent_at = CASE WHEN $3 > 0 THEN coalesce(sent_at, NOW()) ELSE sent_at END, updated_at = NOW() WHERE id = $1",
            batch_id, error, sent,
        )
        if sent:
            await _notify_reviewers(batch, sent, user_id)
    except Exception as e:  # noqa: BLE001
        logger.exception("Couldn't send script batch %s", batch_id)
        await execute(
            "UPDATE content_script_batches SET status = 'DRAFT', error = $2, updated_at = NOW() WHERE id = $1",
            batch_id, f"The scripts couldn't be sent: {str(e)[:200] or e.__class__.__name__}. Send it again.",
        )


async def _notify_reviewers(batch: Dict[str, Any], count: int, creator: UUID) -> None:
    details = _details(batch)
    if batch.get("per_script_details"):
        # Scripts can be for different brands and reviewers: tell all of them (no reviewer named on one means every
        # reviewer on its brand).
        rows = await fetch_all("SELECT details FROM content_script_batch_items WHERE batch_id = $1", batch["id"])
        each = [_json(r["details"]) or {} for r in rows]
        brands = list(dict.fromkeys(b for d in each for b in d.get("brand_codes") or []))
        named = [d.get("reviewer_ids") or [] for d in each]
        details = {"brand_codes": brands, "reviewer_ids": [] if any(not n for n in named) else [r for n in named for r in n]}
    reviewers = await subs._review_recipients(post_row(details, batch["title"]), exclude=creator)
    await subs._notify(
        reviewers, f"Scripts to review: {batch['title']}",
        f"{count} script{'' if count == 1 else 's'} are ready for review together.",
        f"/script-review/{batch['id']}", batch["id"],
    )


# --- The reviewer's side -----------------------------------------------------------------------------------------------

async def review(batch_id: UUID, user_id: UUID, req: BatchReviewRequest) -> BatchReviewResult:
    """Decide on any number of the batch's scripts at once, with the same rules as deciding on one script on its own
    page. Each is handled on its own: one that can't be approved yet is reported and left waiting, and the others still
    go through. (Edits, comments and flags are done on each script's own page.)"""
    row = await _load(batch_id)
    if not await _may_see(row, user_id):
        raise HTTPException(status_code=404, detail="Script batch not found")
    if not await _reviewer_may_act(row, user_id):
        raise HTTPException(status_code=403, detail="You can't review this batch")
    items = {i["id"]: dict(i) for i in await fetch_all(_ITEMS_SQL, batch_id)}
    results: List[ItemResult] = []
    seen: set = set()
    for d in req.decisions:
        if d.item_id in seen:
            continue
        seen.add(d.item_id)
        item = items.get(d.item_id)
        if not item:
            results.append(ItemResult(item_id=d.item_id, ok=False, outcome="That script isn't in this batch"))
            continue
        if item_status(item["submission_id"] is not None, item["script_status"]) != "IN_REVIEW":
            results.append(ItemResult(item_id=d.item_id, ok=False, outcome="It isn't waiting for review"))
            continue
        note = (d.note or "").strip()
        if d.decision == "SEND_BACK" and not note:
            results.append(ItemResult(item_id=d.item_id, ok=False, outcome="Say what needs to change"))
            continue
        try:
            await scripts.decide(
                item["submission_id"], user_id,
                ScriptDecisionRequest(decision=d.decision, note=note or None, due_days=req.due_days, due_at=req.due_at),
            )
        except HTTPException as e:
            results.append(ItemResult(item_id=d.item_id, ok=False, outcome=str(e.detail)))
            continue
        except ValidationError:
            results.append(ItemResult(item_id=d.item_id, ok=False, outcome="That decision wasn't valid"))
            continue
        except Exception:
            logger.exception("Couldn't decide on script %s", d.item_id)
            results.append(ItemResult(item_id=d.item_id, ok=False, outcome="The decision couldn't be saved"))
            continue
        results.append(ItemResult(
            item_id=d.item_id, ok=True, outcome="APPROVED" if d.decision == "APPROVE" else "SENT_BACK",
            submission_id=item["submission_id"], reference_code=item["reference_code"],
        ))
    approved = [r for r in results if r.outcome == "APPROVED"]
    sent_back = [r for r in results if r.outcome == "SENT_BACK"]
    if approved or sent_back:
        # scripts.decide tells the creator about each script; the audit log records the batch review as one.
        await log_audit_event("script_batch", str(batch_id), "REVIEW_SCRIPT_BATCH", user_id,
                              {"approved": len(approved), "sent_back": len(sent_back)})
    return BatchReviewResult(batch=await _out(await _load(batch_id), user_id), results=results)
