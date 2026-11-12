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
import hashlib
import json
import logging
import os
import re
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional
from uuid import UUID

from fastapi import HTTPException

from models.compliance.script_review_model import (
    ScriptHighlight,
    ScriptResolution,
    ScriptBatchRef,
    ScriptDecisionRequest,
    ScriptQueueItem,
    ScriptReviseRequest,
    ScriptStage,
    ScriptVersion,
)
from postgresql_db.database import execute, fetch_all, fetch_one, get_pool
from services.compliance.media import document_layout
from services.compliance.media import document_text_service
from services.compliance.submissions import review_deadline_service
from services.compliance.submissions.flag_review_service import MIN_APPROVAL_SCORE
from services.compliance.submissions import submission_service as subs
from services.compliance.evaluation.compliance_service import check_compliance, rulebook_version
from services.shared.core.audit_service import log_audit_event

logger = logging.getLogger(__name__)

# Where the creator can still change the script, and where it waits on the creator.
CREATOR_TURN = ("EDITS_PROPOSED", "CHANGES_REQUESTED")

# A sent script is read and scored in the background. Past this, a send that never finished counts as failed (the
# server may have restarted), so the creator can send it again.
PROCESSING_TIMEOUT_SEC = 600
READ_WAIT_SEC = 180
_background: set = set()  # keeps the running sends from being garbage collected


def _send_state(row: Dict[str, Any]) -> Dict[str, Optional[str]]:
    """Where the background send of this post's script is: PROCESSING, FAILED (with why), or neither."""
    raw = row.get("script_send")
    data = json.loads(raw) if isinstance(raw, str) else raw
    if not data:
        return {"status": None, "error": None}
    if data.get("status") == "PROCESSING":
        try:
            # A send resumed after a restart counts from when it was picked up again.
            started = max(datetime.fromisoformat(data[k].replace("Z", "+00:00")) for k in ("started_at", "claimed_at") if data.get(k))
        except (KeyError, ValueError):
            started = None
        if started is None or (datetime.now(timezone.utc) - started).total_seconds() > PROCESSING_TIMEOUT_SEC:
            return {"status": "FAILED", "error": "Sending the script took too long. Send it again."}
    return {"status": data.get("status"), "error": data.get("error")}


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
    check = result.model_dump(mode="json")
    # Same flag as the post's own check: a context rule matched but no AI model could read it, so there's no score.
    check["ai_unavailable"] = result.ai_unavailable
    return check


def check_key(row: Dict[str, Any], text: str) -> str:
    """What a script's check depends on: its wording and the details it's judged by (title, brand, audience, region,
    platforms, rule sets). A check made with the same key and rulebook is still right."""
    parts = {
        "title": (row.get("title") or "").strip(),
        "body": _clean(text),
        "brands": sorted(subs.brands_of(row)),
        "audiences": sorted(subs.audiences_of(row)),
        "region": row.get("region"),
        "platforms": sorted(row.get("platforms") or []),
        "rule_sets": sorted(row.get("rule_sets") or []),
    }
    return hashlib.sha256(json.dumps(parts, sort_keys=True, default=str).encode("utf-8")).hexdigest()


def precheck_record(row: Dict[str, Any], text: str, check: Dict[str, Any]) -> str:
    """A check run before sending, as stored (JSON)."""
    return json.dumps({
        "key": check_key(row, text), "rulebook_version": check.get("rulebook_version"), "check": check,
        "checked_at": datetime.now(timezone.utc).isoformat(),
    })


def still_valid(stored: Any, row: Dict[str, Any], text: str, rulebook: Optional[str]) -> Optional[Dict[str, Any]]:
    """The stored check, if it was made on this wording and these details against the current rulebook."""
    stored = subs._json(stored) or {}
    if not stored.get("check") or stored.get("key") != check_key(row, text):
        return None
    if rulebook and stored.get("rulebook_version") != rulebook:
        return None
    return stored["check"]


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


def _self_review_switched_on() -> bool:
    """Development only: ALLOW_SUPER_ADMIN_SELF_REVIEW=true, and never when APP_ENV is production (so a production
    server that has the flag by mistake still keeps the rule)."""
    flag = os.getenv("ALLOW_SUPER_ADMIN_SELF_REVIEW", "").strip().lower() in {"1", "true", "yes", "on"}
    return flag and os.getenv("APP_ENV", "development").strip().lower() != "production"


async def may_review_own_script(user_id: UUID) -> bool:
    """A super admin may review and approve their own scripts in development (to test the flow alone). Everywhere
    else nobody approves their own script, a super admin included."""
    return _self_review_switched_on() and await subs._is_super_admin(user_id)


async def _reviewer_may_act(row: Dict[str, Any], user_id: UUID) -> bool:
    if not await subs._can_review(user_id):
        return False
    # Nobody approves their own script, a super admin included: another reviewer has to (except in development).
    return row["created_by"] != user_id or await may_review_own_script(user_id)


SELF_REVIEW_MESSAGE = ("Send the script to a reviewer other than yourself: nobody approves their own script, "
                       "a super admin included.")


def only_sent_to_self(reviewer_ids: Any, creator: Any) -> bool:
    """The script would go only to the person who wrote it. A super admin may pick themselves for a post, but nobody
    can approve their own script, so nobody else would see it or be told about it: it would sit in review for good."""
    picked = {str(r) for r in reviewer_ids or []}
    return bool(picked) and picked <= {str(creator)}


async def batch_ref(submission_id: UUID) -> Optional[ScriptBatchRef]:
    """Where the script sits in the batch it was uploaded with, or None when it wasn't part of one."""
    mine = await fetch_one(
        """
        SELECT i.batch_id, b.title FROM content_script_batch_items i
        JOIN content_script_batches b ON b.id = i.batch_id AND b.deleted_at IS NULL
        WHERE i.submission_id = $1
        """,
        submission_id,
    )
    if not mine:
        return None
    siblings = await fetch_all(
        """
        SELECT i.submission_id, s.script_status FROM content_script_batch_items i
        JOIN content_submissions s ON s.id = i.submission_id AND s.deleted_at IS NULL
        WHERE i.batch_id = $1 ORDER BY i.position
        """,
        mine["batch_id"],
    )
    ids = [r["submission_id"] for r in siblings]
    if submission_id not in ids:
        return None
    at = ids.index(submission_id)
    after = siblings[at + 1:] + siblings[:at]
    waiting = next((r["submission_id"] for r in after if r["script_status"] == "IN_REVIEW"), None)
    return ScriptBatchRef(
        batch_id=mine["batch_id"], title=mine["title"], position=at + 1, total=len(ids),
        previous_id=ids[at - 1] if at > 0 else None, next_id=ids[at + 1] if at + 1 < len(ids) else None,
        next_waiting_id=waiting, waiting=sum(1 for r in siblings if r["script_status"] == "IN_REVIEW"),
    )


async def get_stage(submission_id: UUID, user_id: UUID) -> ScriptStage:
    row = await subs._get_owned_row(submission_id, user_id, manager_can_read=True)
    status = _status(row) if subs.script_stage_on(row) else None
    send_state = _send_state(row)
    file = await _script_file(submission_id)
    is_creator = row["created_by"] == user_id or await subs._is_super_admin(user_id)
    editable = row["status"] in subs.EDITABLE_STATUSES
    versions = await _versions(submission_id)
    current_rb = await rulebook_version()
    file_text = _clean(file["doc_text"]) if file and file.get("doc_text") else None
    stored = subs._json(row.get("script_precheck")) or {}
    precheck = stored.get("check") if status in ("DRAFT", "CHANGES_REQUESTED") and file_text else None
    if versions and status in ("IN_REVIEW", "DRAFT", "CHANGES_REQUESTED", "EDITS_PROPOSED"):
        latest = versions[-1]
        if current_rb and (latest.check or {}).get("rulebook_version") != current_rb:
            fresh = await check_text(row, latest.body)
            await execute(
                "UPDATE content_script_versions SET check_result = $3::jsonb WHERE submission_id = $1 AND version_number = $2",
                submission_id, latest.version_number, json.dumps(fresh),
            )
            latest.check = fresh
            await _adopt_check(execute, submission_id, fresh)
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
        can_submit=bool(status) and is_creator and editable and status in ("DRAFT", "CHANGES_REQUESTED")
        and send_state["status"] != "PROCESSING",
        can_review=status == "IN_REVIEW" and await _reviewer_may_act(row, user_id),
        can_respond=bool(status) and is_creator and editable and status in CREATOR_TURN,
        processing=send_state["status"] == "PROCESSING",
        send_error=send_state["error"] if send_state["status"] == "FAILED" else None,
        batch=await batch_ref(submission_id),
        precheck=precheck,
        precheck_stale=bool(precheck) and still_valid(stored, row, file_text or "", current_rb) is None,
        precheck_at=stored.get("checked_at") if precheck else None,
    )


async def _adopt_check(run, submission_id: UUID, check: Dict[str, Any]) -> None:
    """While a script is with the reviewer, the post's own score is the script's check, so the creator and the reviewer
    never see two different scores for the same wording. `run` is conn.execute or the pool's execute."""
    await run(
        "UPDATE content_submissions SET latest_check = coalesce(latest_check, '{}'::jsonb) || $2::jsonb WHERE id = $1",
        submission_id, json.dumps({**check, "analysis_pending": False, "documents_pending": 0}),
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
    await _adopt_check(conn.execute, row["id"], check)
    return number


async def _lock_status(conn, submission_id: UUID, allowed: tuple) -> None:
    """Lock the post and make sure the script is still where the caller saw it (two people can act at once)."""
    current = await conn.fetchval("SELECT script_status FROM content_submissions WHERE id = $1 FOR UPDATE", submission_id)
    if (current or "DRAFT") not in allowed:
        raise HTTPException(status_code=409, detail="The script's status just changed; refresh to see it")


async def _send_to_reviewers(row: Dict[str, Any], version: int, check: Dict[str, Any], user_id: UUID) -> List[UUID]:
    """Tell the reviewers it was sent to; returns who was told."""
    # The creator is told too when they may review their own script (development, super admin).
    reviewers = await subs._review_recipients(row, exclude=None if await may_review_own_script(user_id) else user_id)
    issues = check.get("violations_count") or 0
    score = "no score yet (the AI review couldn't run)" if check.get("ai_unavailable") else f"score {check.get('score')}/100"
    await subs._notify(
        reviewers,
        f"Script to review: {row['reference_code']}",
        f"\"{row['title']}\" script version {version}: {score}, "
        f"{issues} issue{'' if issues == 1 else 's'} found.",
        f"/script-review/scripts/{row['id']}",
        row["id"],
    )
    return reviewers


# What a post needs filled in before its script can be sent (the video and caption come later).
_DETAIL_LABELS = {
    "title": "Title",
    "brand_code": "Brand",
    "account_id": "Publishing account(s)",
    "audience": "Target audience",
    "region": "Region",
    "platforms": "Platform(s)",
    "rule_sets": "Rule sets",
    "approval_needed_by": "Need approval by",
    "reviewer_ids": "Send to reviewers",
}


def _missing_details(row: Dict[str, Any]) -> List[str]:
    """The required submission details still empty, by their names on the form."""
    missing: List[str] = []
    if not (row["title"] or "").strip():
        missing.append("title")
    for field in ("brand_code", "audience", "region"):
        if not row[field]:
            missing.append(field)
    if not row["platforms"]:
        missing.append("platforms")
    if not subs._account_ids(row):
        missing.append("account_id")
    if not row["rule_sets"]:
        missing.append("rule_sets")
    if not row.get("approval_needed_by"):
        missing.append("approval_needed_by")
    if not row.get("reviewer_ids"):
        missing.append("reviewer_ids")
    return [_DETAIL_LABELS[m] for m in missing]


async def _require_not_duplicate(row: Dict[str, Any], body: str, confirm_repost: bool) -> None:
    """The same script, already sent for review for the same place and publish date, can't be sent again; for another
    date it can, once the creator confirms (the same rules as posts, see duplicate_service.find_script)."""
    from services.compliance.submissions import duplicate_service

    report = await duplicate_service.find_script(row, body)
    if report.blocking:
        m = report.blocking
        raise HTTPException(status_code=409, detail={
            "code": "DUPLICATE_SCRIPT",
            "message": f"The same script was already sent for review as {m.reference_code} with the same submission "
                       "details and publish date. Change the brand, account, audience, region or platform so they no "
                       "longer overlap, or change the script.",
            "duplicates": report.model_dump(mode="json"),
        })
    if report.repost and not confirm_repost:
        m = report.repost
        raise HTTPException(status_code=409, detail={
            "code": "REPOST_CONFIRM",
            "message": f"The same script was already sent for review as {m.reference_code} for another publish date. "
                       "Confirm to send it for this date too.",
            "duplicates": report.model_dump(mode="json"),
        })


def _repost_confirmed(row: Dict[str, Any]) -> bool:
    """Whether the creator confirmed sending this script again for another date (kept with the send in progress)."""
    send = row.get("script_send")
    if isinstance(send, str):
        try:
            send = json.loads(send)
        except ValueError:
            send = None
    return bool(isinstance(send, dict) and send.get("confirm_repost"))


async def submit(submission_id: UUID, user_id: UUID, confirm_repost: bool = False) -> ScriptStage:
    """Accept the uploaded script for review and answer at once. Reading it, calculating its compliance score and
    telling the reviewers happen in the background (_process_send), so a long script doesn't keep the creator waiting.
    The post shows it as processing until then, or says which step failed."""
    row = await subs._get_owned_row(submission_id, user_id)
    subs._require_editable(row)
    _require_stage(row)
    if _status(row) not in ("DRAFT", "CHANGES_REQUESTED"):
        raise HTTPException(status_code=409, detail="The script is already with a reviewer or approved")
    if _send_state(row)["status"] == "PROCESSING":
        raise HTTPException(status_code=409, detail="Your script is already being processed.")
    missing = _missing_details(row)
    if missing:
        raise HTTPException(status_code=422, detail=f"Fill in the submission details first: {', '.join(missing)}.")
    if only_sent_to_self(row.get("reviewer_ids"), row["created_by"]) and not await may_review_own_script(row["created_by"]):
        raise HTTPException(status_code=422, detail=SELF_REVIEW_MESSAGE)
    file = await _script_file(submission_id)
    if not file:
        raise HTTPException(status_code=422, detail="Upload the script first (PDF, Word or text)")
    # Usually the script's text is already read (on upload): check for a duplicate now, so the creator can confirm a
    # repost straight away. Otherwise it's checked once it's read, in the background.
    if file["doc_text_status"] == "COMPLETED" and (file["doc_text"] or "").strip():
        await _require_not_duplicate(row, _clean(file["doc_text"]), confirm_repost)
    await execute(
        "UPDATE content_submissions SET script_send = $2::jsonb WHERE id = $1",
        submission_id, json.dumps({"status": "PROCESSING", "started_at": datetime.now(timezone.utc).isoformat(),
                                   "confirm_repost": confirm_repost}),
    )
    task = asyncio.create_task(_process_send(submission_id, user_id))
    _background.add(task)
    task.add_done_callback(_background.discard)
    return await get_stage(submission_id, user_id)


async def _fail_send(submission_id: UUID, user_id: UUID, step: str, detail: Any) -> None:
    """The background send couldn't finish: record which step failed and why, and tell the creator."""
    message = f"Error {step}: {detail}"
    logger.warning("Script send failed for %s: %s", submission_id, message)
    row = await fetch_one(
        """
        UPDATE content_submissions SET script_send = $2::jsonb
        WHERE id = $1 AND script_send->>'status' = 'PROCESSING'
        RETURNING reference_code, title
        """,
        submission_id, json.dumps({"status": "FAILED", "error": message, "at": datetime.now(timezone.utc).isoformat()}),
    )
    if row:
        await subs._notify(
            [user_id], f"{row['reference_code']}: the script couldn't be sent", f"\"{row['title']}\": {message}",
            f"/submissions/{submission_id}", submission_id,
        )


async def resume_interrupted_sends() -> None:
    """A send runs inside the server, so a restart cuts it off and leaves the post "processing". On start, pick those
    up again. Each is claimed first, so with several server processes only one of them resumes it."""
    rows = await fetch_all(
        """
        UPDATE content_submissions
        SET script_send = script_send || jsonb_build_object('claimed_at', to_char(NOW() AT TIME ZONE 'utc', 'YYYY-MM-DD"T"HH24:MI:SS"Z"'))
        WHERE script_send->>'status' = 'PROCESSING' AND deleted_at IS NULL AND script_status IN ('DRAFT', 'CHANGES_REQUESTED')
          AND (script_send->>'claimed_at' IS NULL OR (script_send->>'claimed_at')::timestamptz < NOW() - INTERVAL '2 minutes')
        RETURNING id, created_by
        """
    )
    for r in rows:
        logger.info("Resuming the script send for %s that a restart interrupted", r["id"])
        task = asyncio.create_task(_process_send(r["id"], r["created_by"]))
        _background.add(task)
        task.add_done_callback(_background.discard)


async def _read_uploaded(submission_id: UUID) -> tuple:
    """The uploaded script file and its text, read now if it hasn't been yet."""
    file = await _script_file(submission_id)
    if not file:
        raise HTTPException(status_code=422, detail="Upload the script first (PDF, Word or text)")
    if file["doc_text_status"] in (None, "PENDING", "PROCESSING"):
        await document_text_service.read_file(file["id"])
        for _ in range(READ_WAIT_SEC * 2):
            file = await _script_file(submission_id)
            if not file or file["doc_text_status"] not in (None, "PENDING", "PROCESSING"):
                break
            await asyncio.sleep(0.5)
    if not file or file["doc_text_status"] in (None, "PENDING", "PROCESSING"):
        raise HTTPException(status_code=409, detail="The script is taking too long to read. Try again.")
    if file["doc_text_status"] != "COMPLETED" or not (file["doc_text"] or "").strip():
        raise HTTPException(status_code=422, detail=file["doc_text_error"] or "The script's text couldn't be read")
    return file, _clean(file["doc_text"])


async def precheck(submission_id: UUID, user_id: UUID) -> ScriptStage:
    """The creator checks the uploaded script against the rulebook before sending it, to see the score and the flags
    and fix them first. The result is kept and reused when the script is sent unchanged."""
    row = await subs._get_owned_row(submission_id, user_id)
    subs._require_editable(row)
    _require_stage(row)
    if _status(row) not in ("DRAFT", "CHANGES_REQUESTED"):
        raise HTTPException(status_code=409, detail="The script is already with a reviewer or approved")
    _, body = await _read_uploaded(submission_id)
    check = await check_text(row, body)
    await execute("UPDATE content_submissions SET script_precheck = $2::jsonb WHERE id = $1",
                  submission_id, precheck_record(row, body, check))
    return await get_stage(submission_id, user_id)


async def _process_send(submission_id: UUID, user_id: UUID) -> None:
    """Read the uploaded script, calculate its compliance score and send it to the reviewers."""
    step = "reading the script"
    try:
        row = await subs._get_owned_row(submission_id, user_id)
        file, body = await _read_uploaded(submission_id)
        step = "sending the script"
        previous = await _versions(submission_id)
        if previous and _status(row) == "CHANGES_REQUESTED" and _same(previous[-1].body, body):
            raise HTTPException(status_code=422, detail="This is the same script the reviewer sent back. Make the changes first.")
        step = "checking for duplicates"
        try:
            await _require_not_duplicate(row, body, _repost_confirmed(row))
        except HTTPException as dup:
            # The creator isn't on the page to confirm: say what matched; sending again asks them to confirm.
            message = dup.detail.get("message") if isinstance(dup.detail, dict) else str(dup.detail)
            if isinstance(dup.detail, dict) and dup.detail.get("code") == "REPOST_CONFIRM":
                message = message.replace("Confirm to send it for this date too.",
                                          "Send it again and confirm to send it for this date too.")
            raise HTTPException(status_code=409, detail=message)
        step = "calculating the compliance score"
        # The creator's own check of this same script is reused; otherwise it's checked now.
        check = still_valid(row.get("script_precheck"), row, body, await rulebook_version()) or await check_text(row, body)
        step = "sending the script"
        await _send_checked(row, body, "UPLOAD", user_id, check, file_id=file["id"])
    except HTTPException as exc:
        await _fail_send(submission_id, user_id, step, exc.detail)
    except Exception as exc:  # noqa: BLE001 - whatever went wrong, the creator is told rather than left waiting
        logger.exception("Script send crashed for %s", submission_id)
        await _fail_send(submission_id, user_id, step, str(exc) or "something went wrong on our side")


async def _send(row: Dict[str, Any], body: str, source: str, user_id: UUID, file_id: Optional[UUID] = None,
                note: Optional[str] = None) -> ScriptStage:
    check = await check_text(row, body)
    return await _send_checked(row, body, source, user_id, check, file_id, note)


async def _send_checked(row: Dict[str, Any], body: str, source: str, user_id: UUID, check: Dict[str, Any],
                        file_id: Optional[UUID] = None, note: Optional[str] = None, notify: bool = True) -> ScriptStage:
    """Send a checked script for review. notify=False leaves telling people to the caller (a batch tells the reviewers
    once for all its scripts, not once per script)."""
    pool = get_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            await _lock_status(conn, row["id"], ("DRAFT",) + CREATOR_TURN)
            version = await _add_version(conn, row, body, source, user_id, check, file_id)
            await conn.execute(
                """
                UPDATE content_submissions
                SET script_status = 'IN_REVIEW', script_send = NULL, updated_at = NOW(), updated_by = $2,
                    due_at = NULL, due_kind = NULL, due_reminded_at = NULL, due_overdue_notified_at = NULL
                WHERE id = $1
                """,
                row["id"], user_id,
            )
            score = "no score yet" if check.get("ai_unavailable") else f"score {check.get('score')}/100"
            await subs._add_event(
                conn, row["id"], "SCRIPT_SUBMITTED",
                f"Script version {version} sent for review ({score})"
                + (f": {note.strip()}" if note and note.strip() else ""),
                user_id, {"script_version": version, "score": check.get("score")},
            )
    await log_audit_event("content_submission", str(row["id"]), "SUBMIT_SCRIPT", user_id,
                          {"reference_code": row["reference_code"], "script_version": version})
    if not notify:
        return await get_stage(row["id"], user_id)
    reviewers = await _send_to_reviewers(row, version, check, user_id)
    # The creator's own confirmation, as a post gets "submitted for review" (sent once the send has gone through).
    who = f"{len(reviewers)} reviewer{'' if len(reviewers) == 1 else 's'}" if reviewers else "the reviewers"
    await subs._notify(
        [user_id],
        f"Script sent for review: {row['reference_code']}",
        f"\"{row['title']}\" script version {version} is with {who}: {score}.",
        f"/submissions/{row['id']}",
        row["id"],
    )
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
    if req.decision == "APPROVE" and (sent.check or {}).get("ai_unavailable"):
        raise HTTPException(
            status_code=422,
            detail="The AI review couldn't run on this script, so it has no compliance score and can't be approved yet. "
                   "It's re-checked automatically once the AI is back. You can still send it back.",
        )
    if req.decision == "APPROVE":
        # The same standing a post needs: no open critical flag, and a score that reaches the minimum.
        sent_check = sent.check or {}
        critical = [v for v in sent_check.get("violations") or [] if v.get("traffic_light") == "RED"]
        if critical:
            raise HTTPException(
                status_code=422,
                detail=f"{len(critical)} critical compliance flag{'s are' if len(critical) > 1 else ' is'} on this script. "
                       "Send it back with what to change, or edit the wording yourself.",
            )
        if sent_check.get("score") is not None and int(sent_check["score"]) < MIN_APPROVAL_SCORE:
            raise HTTPException(
                status_code=422,
                detail=f"The script's rulebook score is {sent_check['score']}/100; approval needs at least "
                       f"{MIN_APPROVAL_SCORE}. Send it back with what to change, or edit the wording yourself.",
            )
    if req.decision == "APPROVE" and has_edits:
        raise HTTPException(status_code=422,
                            detail="You changed the script, so it goes back to the creator to accept. Send it back instead.")
    if req.decision == "SEND_BACK" and not has_edits and not note:
        raise HTTPException(status_code=422, detail="Edit the script or say what needs to change")

    check = await check_text(row, edited) if has_edits else None
    # The creator's next deadline: the video once it's approved, the changes if it goes back.
    if req.decision == "APPROVE":
        due_at, due_kind = review_deadline_service.video_due(row["publish_at"], req.due_days, req.due_at), "VIDEO"
    else:
        due_at, due_kind = review_deadline_service.revision_due(row["publish_at"], req.due_days, req.due_at), "REVISION"
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
                """
                UPDATE content_submissions
                SET script_status = $2, updated_at = NOW(), due_at = $3,
                    due_kind = CASE WHEN $3::timestamptz IS NULL THEN NULL ELSE $4 END,
                    due_reminded_at = NULL, due_overdue_notified_at = NULL
                WHERE id = $1
                """,
                submission_id, new_status, due_at, due_kind,
            )
            await subs._add_event(conn, submission_id, f"SCRIPT_{decision}", message + (f": {note}" if note else ""),
                                  user_id, {"script_version": sent.version_number, "new_version": new_version,
                                            "due_at": due_at.isoformat() if due_at else None})
    await log_audit_event("content_submission", str(submission_id), f"SCRIPT_{decision}", user_id,
                          {"reference_code": row["reference_code"], "script_version": sent.version_number})
    by = f" Due {review_deadline_service._when(due_at)}." if due_at else ""
    texts = {
        "APPROVED": ("Script approved", "is approved. You can upload the video now." + (f" Upload it by {review_deadline_service._when(due_at)}." if due_at else "")),
        "EDITED": ("Script edited by your reviewer", "came back with edits. Compare the versions and accept or edit them." + by),
        "CHANGES_REQUESTED": ("Script needs changes", "needs changes before filming. See the reviewer's note." + by),
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
                """
                UPDATE content_submissions
                SET script_status = 'APPROVED', updated_at = NOW(), updated_by = $2,
                    due_at = NULL, due_kind = NULL, due_reminded_at = NULL, due_overdue_notified_at = NULL
                WHERE id = $1
                """,
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
    await _require_not_duplicate(row, body, req.confirm_repost)
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


_refreshing: set = set()  # (post id, version) pairs being re-checked, so a busy list doesn't start the same one twice


async def _refresh_stale_scripts(rows: List[Dict[str, Any]]) -> None:
    """Re-check scripts that were checked against an older rulebook, one at a time, and save the result."""
    for r in rows:
        key = (r["id"], r["version_number"])
        try:
            check = await check_text(r, r["script_body"])
            await execute(
                "UPDATE content_script_versions SET check_result = $3::jsonb WHERE submission_id = $1 AND version_number = $2",
                r["id"], r["version_number"], json.dumps(check),
            )
        except Exception as e:
            logger.warning("Failed to re-check script version in the background: %s", e)
        finally:
            _refreshing.discard(key)


def _refresh_in_background(rows: List[Dict[str, Any]]) -> None:
    fresh = [r for r in rows if (r["id"], r["version_number"]) not in _refreshing]
    if not fresh:
        return
    _refreshing.update((r["id"], r["version_number"]) for r in fresh)
    task = asyncio.create_task(_refresh_stale_scripts(fresh))
    _background.add(task)
    task.add_done_callback(_background.discard)


async def queue(user_id: UUID, tab: str = "review") -> List[ScriptQueueItem]:
    """Scripts for one Content Screener tab, on the brands the viewer works on."""
    if tab not in _QUEUE_TABS:
        raise HTTPException(status_code=422, detail="Unknown queue tab")
    # Independent lookups at once, not one round trip after another.
    team, is_super, is_reviewer, brand_scope, current_rb = await asyncio.gather(
        subs._can_see_team_content(user_id), subs._is_super_admin(user_id), subs._can_review(user_id),
        subs._brand_scope(user_id),
        rulebook_version() if tab in ("review", "sent_back") else asyncio.sleep(0, result=None),
    )
    if not team:
        return []
    own_ok = await may_review_own_script(user_id)  # development: a super admin reviews their own scripts too
    where, order = _QUEUE_TABS[tab]
    rows = await fetch_all(
        f"""
        SELECT s.id, s.reference_code, s.title, s.content_type, s.platforms, s.brand_code, s.brand_codes, s.audience, s.audiences, s.region,
               s.rule_sets, s.created_by, s.approval_needed_by, s.script_status, s.updated_at, u.full_name AS creator_name,
               v.version_number, v.created_at AS sent_at, v.body AS script_body, v.check_result, v.reviewed_at,
               bi.batch_id
        FROM content_submissions s
        JOIN LATERAL (
            SELECT version_number, created_at, body, check_result, reviewed_at FROM content_script_versions sv
            WHERE sv.submission_id = s.id ORDER BY version_number DESC LIMIT 1
        ) v ON true
        LEFT JOIN users u ON u.id = s.created_by
        LEFT JOIN content_script_batch_items bi ON bi.submission_id = s.id
        WHERE {where} AND s.script_required AND s.content_type = 'VIDEO'
          AND s.deleted_at IS NULL
          AND (s.created_by = $2 OR {subs._reviewer_sees("$2", "$1")})
        ORDER BY {order}
        LIMIT 100
        """,
        brand_scope,
        user_id,
    )
    items = []
    stale: List[Dict[str, Any]] = []
    for r in rows:
        check = subs._json(r["check_result"]) or {}
        # Never re-check inside the request: after a rule change every script is outdated, and checking them one by one
        # with the AI made this list take minutes (or never load). Show what's saved, refresh in the background.
        if current_rb and check.get("rulebook_version") != current_rb and r.get("script_body"):
            stale.append(dict(r))
        items.append(ScriptQueueItem(
            id=r["id"], reference_code=r["reference_code"], title=r["title"], platforms=list(r["platforms"] or []),
            brand_code=r["brand_code"], brand_codes=subs.brands_of(r), audience=r["audience"], audiences=subs.audiences_of(r), creator_name=r["creator_name"],
            created_by=r["created_by"], version_number=r["version_number"], sent_at=r["sent_at"],
            score=None if check.get("ai_unavailable") else check.get("score"), ai_unavailable=bool(check.get("ai_unavailable")),
            traffic_light=check.get("traffic_light"),
            approval_needed_by=r["approval_needed_by"], script_status=r["script_status"],
            decided_at=r["reviewed_at"] or (r["updated_at"] if r["script_status"] != "IN_REVIEW" else None),
            can_review=r["script_status"] == "IN_REVIEW" and is_reviewer and (r["created_by"] != user_id or own_ok),
            batch_id=r.get("batch_id"),
        ))
    _refresh_in_background(stale)
    return items


async def get_highlights(submission_id: UUID, user_id: UUID, version: int) -> List[ScriptHighlight]:
    """The reviewer's highlights and notes on a script version, for anyone who can read the post."""
    await subs._get_owned_row(submission_id, user_id, manager_can_read=True)
    row = await fetch_one(
        "SELECT highlights FROM content_script_highlights WHERE submission_id = $1 AND version_number = $2",
        submission_id, version,
    )
    items = row["highlights"] if row else []
    if isinstance(items, str):
        items = json.loads(items)
    return [ScriptHighlight(**h) for h in items or []]


async def save_highlights(submission_id: UUID, user_id: UUID, version: int, highlights: List[ScriptHighlight]) -> List[ScriptHighlight]:
    """Replace the highlights on a script version. Reviewers only."""
    row = await subs._get_owned_row(submission_id, user_id, manager_can_read=True)
    _require_stage(row)
    if not await _reviewer_may_act(row, user_id):
        raise HTTPException(status_code=403, detail="Only a reviewer can highlight the script")
    exists = await fetch_one(
        "SELECT 1 FROM content_script_versions WHERE submission_id = $1 AND version_number = $2", submission_id, version
    )
    if not exists:
        raise HTTPException(status_code=404, detail="That script version doesn't exist")
    await execute(
        """
        INSERT INTO content_script_highlights (submission_id, version_number, highlights, updated_by, updated_at)
        VALUES ($1, $2, $3::jsonb, $4, NOW())
        ON CONFLICT (submission_id, version_number)
        DO UPDATE SET highlights = EXCLUDED.highlights, updated_by = EXCLUDED.updated_by, updated_at = NOW()
        """,
        submission_id, version, json.dumps([h.model_dump() for h in highlights]), user_id,
    )
    return highlights


async def resolve_highlights(submission_id: UUID, user_id: UUID, version: int, resolutions: List[ScriptResolution]) -> List[ScriptHighlight]:
    """The creator marks which of the reviewer's highlights they fixed, and what the wording reads now.

    Only those two fields change, and only on highlights that exist; the reviewer's own marks stay as they were."""
    row = await subs._get_owned_row(submission_id, user_id, manager_can_read=True)
    _require_stage(row)
    if row["created_by"] != user_id and not await subs._is_super_admin(user_id):
        raise HTTPException(status_code=403, detail="Only the post's creator can mark highlights done")
    current = await get_highlights(submission_id, user_id, version)
    by_id = {r.id: r for r in resolutions}
    merged = [
        h.model_copy(update={"done": by_id[h.id].done, "updated": by_id[h.id].updated if by_id[h.id].done else None}) if h.id in by_id else h
        for h in current
    ]
    await execute(
        """
        INSERT INTO content_script_highlights (submission_id, version_number, highlights, updated_by, updated_at)
        VALUES ($1, $2, $3::jsonb, $4, NOW())
        ON CONFLICT (submission_id, version_number)
        DO UPDATE SET highlights = EXCLUDED.highlights, updated_by = EXCLUDED.updated_by, updated_at = NOW()
        """,
        submission_id, version, json.dumps([h.model_dump() for h in merged]), user_id,
    )
    return merged
