"""Content Submission Service
Drafts, file storage, versioning, review jobs and activity history for content submissions.
"""

import json
import logging
import mimetypes
import os
import re
import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from uuid import UUID, uuid4

import aiofiles
from fastapi import HTTPException, UploadFile, status

from models.compliance_model import ComplianceCheckRequest
from models.notification_model import NotificationCreate
from models.submission_model import (
    AUDIENCES,
    PLATFORMS,
    PROVIDER_PLATFORMS,
    REGIONS,
    REVIEWER_ROUTES,
    RULE_SETS,
    AccountOption,
    BrandOption,
    ReviewJob,
    ReviewQueueItem,
    Submission,
    SubmissionCreate,
    SubmissionEvent,
    SubmissionFile,
    SubmissionHistory,
    SubmissionOptions,
    SubmissionSummary,
    SubmissionUpdate,
    SubmissionVersion,
)
from postgresql_db.database import fetch_all, fetch_one, get_pool
from services.audit_service import log_audit_event
from services import ai_usage_service, sentiment_service, transcription_service
from services.ai_usage_service import AiLimitExceeded
from services.compliance_service import check_compliance

logger = logging.getLogger(__name__)

EDITABLE_STATUSES = ("DRAFT", "CHANGES_REQUESTED")
STORAGE_ROOT = Path(os.getenv("SUBMISSION_STORAGE_DIR", ".data/submission_files")).resolve()

_MB = 1024 * 1024
MAX_BYTES = {
    "MAIN": int(os.getenv("SUBMISSION_MAX_MAIN_MB", "2048")) * _MB,
    "SCRIPT": 25 * _MB,
    "IMAGE": 50 * _MB,
    "SUPPORTING": 100 * _MB,
}
IMAGE_EXT = {".jpg", ".jpeg", ".png", ".webp", ".gif"}
VIDEO_EXT = {".mp4", ".mov", ".avi", ".webm", ".mkv", ".m4v"}
DOC_EXT = {".pdf", ".doc", ".docx", ".txt", ".md"}
ALLOWED_EXT = {
    "SCRIPT": {".pdf", ".doc", ".docx", ".txt"},
    "IMAGE": IMAGE_EXT,
    "SUPPORTING": DOC_EXT | {".xls", ".xlsx", ".csv", ".zip", ".ppt", ".pptx"} | IMAGE_EXT,
}
MAIN_EXT_BY_TYPE = {
    "VIDEO": VIDEO_EXT,
    "IMAGE": IMAGE_EXT,
    "CAROUSEL": IMAGE_EXT,
    "SOCIAL_POST": IMAGE_EXT | VIDEO_EXT,
    "BLOG": DOC_EXT,
}
# Types whose main asset is required to submit; posts and articles can be text-only.
MAIN_FILE_REQUIRED = {"VIDEO", "IMAGE", "CAROUSEL"}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _json(value: Any) -> Optional[Dict[str, Any]]:
    if value is None:
        return None
    if isinstance(value, str):
        try:
            return json.loads(value)
        except ValueError:
            return None
    return value


def _account_option(row: Dict[str, Any], prefix: str = "") -> Optional[AccountOption]:
    if not row.get(f"{prefix}id"):
        return None
    meta = _json(row.get(f"{prefix}connection_metadata")) or {}
    provider = row[f"{prefix}provider"]
    platform = PROVIDER_PLATFORMS.get(provider.upper(), provider.upper())
    platform_label = {"X": "X", "LINKEDIN": "LinkedIn", "YOUTUBE": "YouTube", "TIKTOK": "TikTok",
                      "INSTAGRAM": "Instagram", "FACEBOOK": "Facebook", "REDDIT": "Reddit"}.get(platform, platform.title())
    return AccountOption(
        id=row[f"{prefix}id"],
        label=f"{row[f'{prefix}account_name']} ({platform_label})",
        account_name=row[f"{prefix}account_name"] or "",
        provider=provider,
        platform=platform,
        handle=meta.get("handle"),
        brand_code=meta.get("brand_code"),
        is_ceo=bool(meta.get("is_ceo")),
        is_demo=bool(meta.get("demo_seed")),
    )


def _file_out(submission_id: UUID, row: Dict[str, Any]) -> SubmissionFile:
    transcript = row.get("transcript")
    if isinstance(transcript, str):
        transcript = json.loads(transcript)
    return SubmissionFile(
        id=row["id"],
        kind=row["kind"],
        original_name=row["original_name"],
        mime_type=row["mime_type"],
        size_bytes=row["size_bytes"],
        created_at=row["created_at"],
        url=f"/api/submissions/{submission_id}/files/{row['id']}",
        transcript_status=row.get("transcript_status"),
        transcript=transcript,
        transcript_error=row.get("transcript_error"),
    )


async def _is_super_admin(user_id: UUID) -> bool:
    row = await fetch_one("SELECT is_super_admin FROM users WHERE id = $1", user_id)
    return bool(row and row["is_super_admin"])


async def _get_owned_row(submission_id: UUID, user_id: UUID) -> Dict[str, Any]:
    """Load a submission the user may access (creator or super admin), with its account joined."""
    row = await fetch_one(
        """
        SELECT s.*,
               c.id AS acc_id, c.provider AS acc_provider, c.account_name AS acc_account_name,
               c.connection_metadata AS acc_connection_metadata
        FROM content_submissions s
        LEFT JOIN oauth_connections c ON c.id = s.account_id
        WHERE s.id = $1
        """,
        submission_id,
    )
    if not row:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Submission not found")
    if row["created_by"] != user_id and not await _is_super_admin(user_id):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Submission not found")
    return row


def _require_editable(row: Dict[str, Any]) -> None:
    if row["status"] not in EDITABLE_STATUSES:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Submission is {row['status'].replace('_', ' ').lower()} and can't be edited. Withdraw it to make changes.",
        )


async def _add_event(conn, submission_id: UUID, event_type: str, message: str, actor: UUID, data: Optional[dict] = None) -> None:
    await conn.execute(
        """
        INSERT INTO content_submission_events (submission_id, event_type, message, data, actor_user_id)
        VALUES ($1, $2, $3, $4::jsonb, $5)
        """,
        submission_id,
        event_type,
        message,
        json.dumps(data, default=str) if data else None,
        actor,
    )


async def _list_files(submission_id: UUID) -> List[Dict[str, Any]]:
    return await fetch_all(
        """
        SELECT id, kind, original_name, mime_type, size_bytes, created_at,
               transcript, transcript_status, transcript_error
        FROM content_submission_files
        WHERE submission_id = $1 AND deleted_at IS NULL
        ORDER BY created_at ASC
        """,
        submission_id,
    )


async def _to_submission(row: Dict[str, Any]) -> Submission:
    files = await _list_files(row["id"])
    return Submission(
        id=row["id"],
        reference_code=row["reference_code"],
        title=row["title"],
        content_type=row["content_type"],
        brand_code=row["brand_code"],
        audience=row["audience"],
        region=row["region"],
        platforms=list(row["platforms"] or []),
        account_id=row["account_id"],
        account=_account_option(row, prefix="acc_"),
        publish_at=row["publish_at"],
        caption_html=row["caption_html"],
        caption_text=row["caption_text"],
        rule_sets=list(row["rule_sets"] or []),
        reviewer_route=list(row["reviewer_route"] or []),
        status=row["status"],
        current_version=row["current_version"],
        latest_check=_json(row["latest_check"]),
        files=[_file_out(row["id"], f) for f in files],
        can_edit=row["status"] in EDITABLE_STATUSES,
        submitted_at=row["submitted_at"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


async def _connected_account(account_id: UUID) -> Optional[Dict[str, Any]]:
    """A publishing account that is connected and not expired, or None."""
    row = await fetch_one(
        """
        SELECT id, provider, account_name, connection_metadata
        FROM oauth_connections
        WHERE id = $1 AND status = 'CONNECTED' AND (expires_at IS NULL OR expires_at > NOW())
        """,
        account_id,
    )
    if not row or row["provider"].upper() not in PROVIDER_PLATFORMS:
        return None
    return row


# ---------------------------------------------------------------------------
# Options
# ---------------------------------------------------------------------------

async def get_options() -> SubmissionOptions:
    brands = await fetch_all("SELECT code, name FROM brands WHERE is_active ORDER BY sort_order, name")
    accounts = await fetch_all(
        """
        SELECT id, provider, account_name, connection_metadata
        FROM oauth_connections
        WHERE status = 'CONNECTED'
          AND (expires_at IS NULL OR expires_at > NOW())
          AND upper(provider) = ANY($1::text[])
        ORDER BY account_name, provider
        """,
        list(PROVIDER_PLATFORMS.keys()),
    )
    return SubmissionOptions(
        brands=[BrandOption(code=b["code"], name=b["name"]) for b in brands],
        accounts=[a for a in (_account_option(r) for r in accounts) if a],
    )


# ---------------------------------------------------------------------------
# CRUD
# ---------------------------------------------------------------------------

async def create_submission(data: SubmissionCreate, user_id: UUID) -> Submission:
    pool = get_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            row = await conn.fetchrow(
                """
                INSERT INTO content_submissions (title, content_type, created_by, updated_by)
                VALUES ($1, $2, $3, $3)
                RETURNING id, reference_code
                """,
                data.title.strip(),
                data.content_type,
                user_id,
            )
            await _add_event(conn, row["id"], "CREATED", f"Draft {row['reference_code']} created", user_id)
    await log_audit_event("content_submission", str(row["id"]), "CREATE_SUBMISSION", user_id,
                          {"reference_code": row["reference_code"], "title": data.title})
    return await get_submission(row["id"], user_id)


async def list_submissions(user_id: UUID, status_filter: Optional[str] = None) -> List[SubmissionSummary]:
    rows = await fetch_all(
        """
        SELECT s.id, s.reference_code, s.title, s.content_type, s.brand_code, s.platforms, s.status,
               s.current_version, s.publish_at, s.submitted_at, s.created_at, s.updated_at, s.audience,
               left(s.caption_text, 600) AS caption_text,
               s.latest_check->>'traffic_light' AS latest_traffic_light,
               (s.latest_check->>'score')::int AS latest_score,
               (SELECT count(*) FROM content_submission_files f
                 WHERE f.submission_id = s.id AND f.deleted_at IS NULL) AS file_count,
               c.id AS acc_id, c.provider AS acc_provider, c.account_name AS acc_account_name,
               c.connection_metadata AS acc_connection_metadata,
               cov.id AS cov_id, cov.kind AS cov_kind, cov.original_name AS cov_original_name,
               cov.mime_type AS cov_mime_type, cov.size_bytes AS cov_size_bytes, cov.created_at AS cov_created_at
        FROM content_submissions s
        LEFT JOIN oauth_connections c ON c.id = s.account_id
        -- Thumbnail: the main video/image, otherwise the first attached image.
        LEFT JOIN LATERAL (
            SELECT * FROM content_submission_files cf
            WHERE cf.submission_id = s.id AND cf.deleted_at IS NULL
              AND (cf.mime_type LIKE 'video/%' OR cf.mime_type LIKE 'image/%')
            ORDER BY (cf.kind = 'MAIN') DESC, cf.created_at ASC
            LIMIT 1
        ) cov ON true
        WHERE s.created_by = $1 AND ($2::text IS NULL OR s.status = $2)
        -- Work on a deadline comes first: changes to fix, then drafts to finish, then content waiting
        -- on reviewers, each by nearest publish date; finished content goes last.
        ORDER BY CASE s.status
                     WHEN 'CHANGES_REQUESTED' THEN 0
                     WHEN 'DRAFT' THEN 1
                     WHEN 'IN_REVIEW' THEN 2
                     ELSE 3
                 END,
                 s.publish_at ASC NULLS LAST,
                 s.updated_at DESC
        LIMIT 200
        """,
        user_id,
        status_filter,
    )
    summaries = []
    for r in rows:
        base = {k: v for k, v in r.items() if not k.startswith(("acc_", "cov_")) and k != "platforms"}
        cover = {k[4:]: v for k, v in r.items() if k.startswith("cov_")}
        summaries.append(SubmissionSummary(
            **base,
            platforms=list(r["platforms"] or []),
            account=_account_option(r, prefix="acc_"),
            cover=_file_out(r["id"], cover) if cover["id"] else None,
        ))
    return summaries


async def get_submission(submission_id: UUID, user_id: UUID) -> Submission:
    return await _to_submission(await _get_owned_row(submission_id, user_id))


async def update_submission(submission_id: UUID, data: SubmissionUpdate, user_id: UUID) -> Submission:
    row = await _get_owned_row(submission_id, user_id)
    _require_editable(row)

    fields: Dict[str, Any] = {}
    payload = data.model_dump(exclude_unset=True, exclude={"clear_fields"})

    if "title" in payload and payload["title"] is not None:
        fields["title"] = payload["title"].strip()
    if payload.get("content_type"):
        fields["content_type"] = payload["content_type"]
    if payload.get("brand_code"):
        brand = await fetch_one("SELECT code FROM brands WHERE code = $1 AND is_active", payload["brand_code"])
        if not brand:
            raise HTTPException(status_code=422, detail="Unknown brand")
        fields["brand_code"] = brand["code"]
    if payload.get("audience"):
        if payload["audience"] not in AUDIENCES:
            raise HTTPException(status_code=422, detail="Unknown target audience")
        fields["audience"] = payload["audience"]
    if payload.get("region"):
        if payload["region"] not in REGIONS:
            raise HTTPException(status_code=422, detail="Unknown region")
        fields["region"] = payload["region"]
    if payload.get("platforms") is not None:
        bad = [p for p in payload["platforms"] if p not in PLATFORMS]
        if bad:
            raise HTTPException(status_code=422, detail=f"Unknown platform(s): {', '.join(bad)}")
        fields["platforms"] = list(dict.fromkeys(payload["platforms"]))
    if payload.get("account_id"):
        if not await _connected_account(payload["account_id"]):
            raise HTTPException(status_code=422, detail="That publishing account isn't connected")
        fields["account_id"] = payload["account_id"]
    if payload.get("publish_at"):
        fields["publish_at"] = payload["publish_at"]
    if "caption_html" in payload:
        fields["caption_html"] = payload["caption_html"]
    if "caption_text" in payload:
        fields["caption_text"] = payload["caption_text"]
    if payload.get("rule_sets") is not None:
        bad = [r for r in payload["rule_sets"] if r not in RULE_SETS]
        if bad:
            raise HTTPException(status_code=422, detail=f"Unknown rule set(s): {', '.join(bad)}")
        fields["rule_sets"] = list(dict.fromkeys(payload["rule_sets"]))
    if payload.get("reviewer_route") is not None:
        bad = [r for r in payload["reviewer_route"] if r not in REVIEWER_ROUTES]
        if bad:
            raise HTTPException(status_code=422, detail=f"Unknown reviewer route(s): {', '.join(bad)}")
        fields["reviewer_route"] = list(dict.fromkeys(payload["reviewer_route"]))
    for name in data.clear_fields:
        fields[name] = None

    if not fields:
        return await _to_submission(row)

    columns = list(fields.keys())
    sets = ", ".join(f"{c} = ${i + 2}" for i, c in enumerate(columns))
    params = [submission_id, *fields.values(), user_id]
    pool = get_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            await conn.execute(
                f"UPDATE content_submissions SET {sets}, updated_by = ${len(params)}, updated_at = NOW() WHERE id = $1",
                *params,
            )
            # One "draft saved" entry per 10 minutes keeps autosave from flooding the activity log.
            await conn.execute(
                """
                INSERT INTO content_submission_events (submission_id, event_type, message, actor_user_id)
                SELECT $1, 'DRAFT_SAVED', 'Draft saved', $2
                WHERE NOT EXISTS (
                    SELECT 1 FROM content_submission_events
                    WHERE submission_id = $1 AND event_type = 'DRAFT_SAVED' AND created_at > NOW() - INTERVAL '10 minutes'
                )
                """,
                submission_id,
                user_id,
            )
    return await get_submission(submission_id, user_id)


async def delete_draft(submission_id: UUID, user_id: UUID) -> None:
    row = await _get_owned_row(submission_id, user_id)
    if row["status"] != "DRAFT" or row["current_version"] > 0:
        raise HTTPException(status_code=409, detail="Only drafts that were never submitted can be deleted")
    pool = get_pool()
    async with pool.acquire() as conn:
        await conn.execute("DELETE FROM content_submissions WHERE id = $1", submission_id)
    # Never-submitted drafts have no version history that could reference these files.
    shutil.rmtree(STORAGE_ROOT / str(submission_id), ignore_errors=True)
    await log_audit_event("content_submission", str(submission_id), "DELETE_SUBMISSION_DRAFT", user_id,
                          {"reference_code": row["reference_code"]})


# ---------------------------------------------------------------------------
# Files
# ---------------------------------------------------------------------------

def _safe_name(name: str) -> str:
    base = os.path.basename(name or "file")
    base = re.sub(r"[^A-Za-z0-9._ -]", "_", base).strip() or "file"
    return base[:200]


async def add_file(submission_id: UUID, kind: str, upload: UploadFile, user_id: UUID) -> SubmissionFile:
    row = await _get_owned_row(submission_id, user_id)
    _require_editable(row)
    if kind not in MAX_BYTES:
        raise HTTPException(status_code=422, detail="Unknown file kind")

    original_name = _safe_name(upload.filename or "file")
    ext = Path(original_name).suffix.lower()
    allowed = MAIN_EXT_BY_TYPE.get(row["content_type"], set()) if kind == "MAIN" else ALLOWED_EXT[kind]
    if ext not in allowed:
        raise HTTPException(
            status_code=415,
            detail=f"{ext or 'This file type'} isn't allowed here. Allowed: {', '.join(sorted(allowed))}",
        )

    file_id = uuid4()
    folder = STORAGE_ROOT / str(submission_id)
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"{file_id}{ext}"
    limit = MAX_BYTES[kind]
    size = 0
    try:
        async with aiofiles.open(path, "wb") as out:
            while chunk := await upload.read(1024 * 1024):
                size += len(chunk)
                if size > limit:
                    raise HTTPException(status_code=413, detail=f"File is larger than {limit // _MB} MB")
                await out.write(chunk)
    except BaseException:
        path.unlink(missing_ok=True)
        raise
    if size == 0:
        path.unlink(missing_ok=True)
        raise HTTPException(status_code=422, detail="The file is empty")

    mime = upload.content_type or mimetypes.guess_type(original_name)[0] or "application/octet-stream"
    needs_transcript = kind == "MAIN" and mime.startswith("video/")
    # Over the AI limit the upload still works; the transcript is marked failed and can be retried later.
    transcript_blocked: Optional[str] = None
    if needs_transcript:
        try:
            await ai_usage_service.reserve("TRANSCRIPTION", user_id, submission_id)
        except AiLimitExceeded as e:
            transcript_blocked = f"Not transcribed: {e.detail}"
    pool = get_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            # Single-asset types keep one main file: replacing it soft-deletes the previous one.
            if kind == "MAIN" and row["content_type"] != "CAROUSEL":
                await conn.execute(
                    """
                    UPDATE content_submission_files SET deleted_at = NOW()
                    WHERE submission_id = $1 AND kind = 'MAIN' AND deleted_at IS NULL
                    """,
                    submission_id,
                )
            if kind == "SCRIPT":
                await conn.execute(
                    """
                    UPDATE content_submission_files SET deleted_at = NOW()
                    WHERE submission_id = $1 AND kind = 'SCRIPT' AND deleted_at IS NULL
                    """,
                    submission_id,
                )
            saved = await conn.fetchrow(
                """
                INSERT INTO content_submission_files
                    (id, submission_id, kind, original_name, mime_type, size_bytes, storage_path, uploaded_by,
                     transcript_status, transcript_error, transcript_updated_at)
                VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, CASE WHEN $9::text IS NULL THEN NULL ELSE NOW() END)
                RETURNING id, kind, original_name, mime_type, size_bytes, created_at, transcript_status, transcript_error
                """,
                file_id,
                submission_id,
                kind,
                original_name,
                mime,
                size,
                str(path.relative_to(STORAGE_ROOT)),
                user_id,
                ("FAILED" if transcript_blocked else "PENDING") if needs_transcript else None,
                transcript_blocked,
            )
            await conn.execute("UPDATE content_submissions SET updated_at = NOW(), updated_by = $2 WHERE id = $1",
                               submission_id, user_id)
            await _add_event(conn, submission_id, "FILE_ADDED", f"Added {original_name}", user_id,
                             {"file_id": str(file_id), "kind": kind, "size_bytes": size})
    if needs_transcript and not transcript_blocked:
        transcription_service.schedule([file_id])
    return _file_out(submission_id, dict(saved))


async def retranscribe(submission_id: UUID, file_id: UUID, user_id: UUID) -> SubmissionFile:
    """Generate the speech transcript again, e.g. after a failed attempt."""
    await _get_owned_row(submission_id, user_id)
    busy = await fetch_one(
        "SELECT 1 FROM content_submission_files WHERE id = $1 AND submission_id = $2 AND transcript_status = 'PROCESSING'",
        file_id, submission_id,
    )
    if not busy:
        await ai_usage_service.reserve("TRANSCRIPTION", user_id, submission_id)
    f = await fetch_one(
        """
        UPDATE content_submission_files
        SET transcript_status = 'PENDING', transcript_error = NULL, transcript_updated_at = NOW()
        WHERE id = $1 AND submission_id = $2 AND deleted_at IS NULL AND mime_type LIKE 'video/%'
          AND transcript_status IS DISTINCT FROM 'PROCESSING'
        RETURNING id, kind, original_name, mime_type, size_bytes, created_at,
                  transcript, transcript_status, transcript_error
        """,
        file_id,
        submission_id,
    )
    if not f:
        raise HTTPException(status_code=409, detail="This video is already being transcribed, or it isn't a video file.")
    transcription_service.schedule([file_id])
    return _file_out(submission_id, f)


async def remove_file(submission_id: UUID, file_id: UUID, user_id: UUID) -> None:
    row = await _get_owned_row(submission_id, user_id)
    _require_editable(row)
    pool = get_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            # Soft delete keeps the file for earlier versions' history.
            name = await conn.fetchval(
                """
                UPDATE content_submission_files SET deleted_at = NOW()
                WHERE id = $1 AND submission_id = $2 AND deleted_at IS NULL
                RETURNING original_name
                """,
                file_id,
                submission_id,
            )
            if not name:
                raise HTTPException(status_code=404, detail="File not found")
            await _add_event(conn, submission_id, "FILE_REMOVED", f"Removed {name}", user_id, {"file_id": str(file_id)})


async def get_file_for_download(submission_id: UUID, file_id: UUID, user_id: UUID) -> Tuple[Path, str, str]:
    await _get_owned_row(submission_id, user_id)
    f = await fetch_one(
        "SELECT original_name, mime_type, storage_path FROM content_submission_files WHERE id = $1 AND submission_id = $2",
        file_id,
        submission_id,
    )
    if not f:
        raise HTTPException(status_code=404, detail="File not found")
    path = (STORAGE_ROOT / f["storage_path"]).resolve()
    if STORAGE_ROOT not in path.parents or not path.is_file():
        raise HTTPException(status_code=404, detail="File not found")
    return path, f["mime_type"], f["original_name"]


# ---------------------------------------------------------------------------
# Checks, submission and history
# ---------------------------------------------------------------------------

def _check_text(row: Dict[str, Any]) -> str:
    return "\n\n".join(p for p in [row["title"], row["caption_text"]] if p and p.strip())


async def run_initial_check(submission_id: UUID, user_id: UUID) -> Submission:
    row = await _get_owned_row(submission_id, user_id)
    text = _check_text(row)
    if not text:
        raise HTTPException(status_code=422, detail="Add a title or caption before running the check")
    result = await check_compliance(ComplianceCheckRequest(
        text=text,
        video_title=row["title"],
        content_type=f"{row['content_type']}_{(row['platforms'] or ['GENERAL'])[0]}",
        rule_sets=list(row["rule_sets"] or []),
        brand_code=row["brand_code"],
        audience=row["audience"],
        region=row["region"],
        platforms=list(row["platforms"] or []),
    ))
    check = {**result.model_dump(mode="json"), "checked_at": datetime.now(timezone.utc).isoformat()}
    pool = get_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            await conn.execute("UPDATE content_submissions SET latest_check = $2::jsonb, updated_at = NOW() WHERE id = $1",
                               submission_id, json.dumps(check))
            await _add_event(conn, submission_id, "CHECK_RUN",
                             f"Initial check: {result.violations_count} issue(s), score {result.score}/100", user_id,
                             {"traffic_light": result.traffic_light, "score": result.score})
    return await get_submission(submission_id, user_id)


async def _missing_for_submit(row: Dict[str, Any]) -> List[str]:
    missing: List[str] = []
    if not (row["title"] or "").strip():
        missing.append("title")
    for field in ("brand_code", "audience", "region"):
        if not row[field]:
            missing.append(field)
    if not row["platforms"]:
        missing.append("platforms")
    if not row["account_id"]:
        missing.append("account_id")
    if not row["rule_sets"]:
        missing.append("rule_sets")
    if not row["reviewer_route"]:
        missing.append("reviewer_route")
    has_main = await fetch_one(
        "SELECT 1 AS ok FROM content_submission_files WHERE submission_id = $1 AND kind = 'MAIN' AND deleted_at IS NULL LIMIT 1",
        row["id"],
    )
    has_text = bool((row["caption_text"] or "").strip())
    if row["content_type"] in MAIN_FILE_REQUIRED and not has_main:
        missing.append("main_file")
    elif not has_main and not has_text:
        missing.append("content")
    return missing


def _review_job(r: Dict[str, Any]) -> ReviewJob:
    return ReviewJob(
        id=r["id"],
        version_number=r["version_number"],
        status=r["status"],
        reviewer_route=list(r["reviewer_route"] or []),
        traffic_light=r["traffic_light"],
        score=r["score"],
        automated_check=_json(r["automated_check"]),
        created_at=r["created_at"],
        completed_at=r["completed_at"],
    )


async def submit_for_review(submission_id: UUID, user_id: UUID) -> Tuple[Submission, ReviewJob]:
    row = await _get_owned_row(submission_id, user_id)
    if row["status"] == "IN_REVIEW":
        raise HTTPException(status_code=409, detail="This submission is already in review")
    _require_editable(row)

    missing = await _missing_for_submit(row)
    if row["account_id"] and not await _connected_account(row["account_id"]):
        missing.append("account_id")
    account = _account_option(row, prefix="acc_")
    if account and row["platforms"] and account.platform not in row["platforms"]:
        raise HTTPException(status_code=422, detail={
            "message": f"The publishing account posts to {account.platform}, which isn't in the selected platforms.",
            "missing": ["platforms"],
        })
    if row["publish_at"] and row["publish_at"] < datetime.now(timezone.utc):
        missing.append("publish_at")
    if missing:
        raise HTTPException(status_code=422, detail={
            "message": "Some required information is missing.",
            "missing": sorted(set(missing)),
        })

    # Automated rulebook screening is the first step of the review job.
    result = await check_compliance(ComplianceCheckRequest(
        text=_check_text(row) or row["title"],
        video_title=row["title"],
        content_type=f"{row['content_type']}_{row['platforms'][0]}",
        rule_sets=list(row["rule_sets"] or []),
        brand_code=row["brand_code"],
        audience=row["audience"],
        region=row["region"],
        platforms=list(row["platforms"] or []),
    ))
    check = {**result.model_dump(mode="json"), "checked_at": datetime.now(timezone.utc).isoformat()}

    files = await _list_files(submission_id)
    version = row["current_version"] + 1
    snapshot = {
        "title": row["title"],
        "content_type": row["content_type"],
        "brand_code": row["brand_code"],
        "audience": row["audience"],
        "region": row["region"],
        "platforms": list(row["platforms"] or []),
        "account": account.model_dump(mode="json") if account else None,
        "publish_at": row["publish_at"].isoformat() if row["publish_at"] else None,
        "caption_html": row["caption_html"],
        "caption_text": row["caption_text"],
        "rule_sets": list(row["rule_sets"] or []),
        "reviewer_route": list(row["reviewer_route"] or []),
        "files": [
            {"id": str(f["id"]), "kind": f["kind"], "original_name": f["original_name"],
             "mime_type": f["mime_type"], "size_bytes": f["size_bytes"]}
            for f in files
        ],
    }

    pool = get_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            # Guard against a double submit racing past the status check above.
            locked = await conn.fetchval(
                "SELECT status FROM content_submissions WHERE id = $1 FOR UPDATE", submission_id
            )
            if locked not in EDITABLE_STATUSES:
                raise HTTPException(status_code=409, detail="This submission was already submitted")
            await conn.execute(
                """
                INSERT INTO content_submission_versions (submission_id, version_number, snapshot, created_by)
                VALUES ($1, $2, $3::jsonb, $4)
                """,
                submission_id, version, json.dumps(snapshot, default=str), user_id,
            )
            job = await conn.fetchrow(
                """
                INSERT INTO content_review_jobs
                    (submission_id, version_number, status, reviewer_route, automated_check, traffic_light, score, created_by)
                VALUES ($1, $2, 'AWAITING_REVIEW', $3, $4::jsonb, $5, $6, $7)
                RETURNING *
                """,
                submission_id, version, list(row["reviewer_route"]), json.dumps(check),
                result.traffic_light, result.score, user_id,
            )
            await conn.execute(
                """
                UPDATE content_submissions
                SET status = 'IN_REVIEW', current_version = $2, latest_check = $3::jsonb,
                    submitted_at = NOW(), updated_at = NOW(), updated_by = $4
                WHERE id = $1
                """,
                submission_id, version, json.dumps(check), user_id,
            )
            await _add_event(conn, submission_id, "SUBMITTED", f"Version {version} submitted for review", user_id,
                             {"version": version, "review_job_id": str(job["id"])})
            await _add_event(conn, submission_id, "CHECK_COMPLETED",
                             f"Automated rulebook check: {result.violations_count} issue(s), score {result.score}/100",
                             user_id, {"traffic_light": result.traffic_light, "score": result.score})
            await _add_event(conn, submission_id, "REVIEW_REQUESTED",
                             f"Sent to {', '.join(row['reviewer_route'])}", user_id,
                             {"reviewer_route": list(row["reviewer_route"])})

    await log_audit_event("content_submission", str(submission_id), "SUBMIT_FOR_REVIEW", user_id,
                          {"reference_code": row["reference_code"], "version": version,
                           "traffic_light": result.traffic_light, "score": result.score})
    try:
        from services.notification_service import create_notification
        await create_notification(NotificationCreate(
            user_id=str(user_id),
            type="SUBMISSION",
            title=f"{row['reference_code']} submitted for review",
            message=f"\"{row['title']}\" (version {version}) is with {', '.join(row['reviewer_route'])}. "
                    f"Automated check: {result.score}/100.",
            link_url=f"/submissions/{submission_id}",
            entity_type="content_submission",
            entity_id=str(submission_id),
        ))
    except Exception:
        logger.warning("Could not create submission notification", exc_info=True)

    # Reviewers get a sentiment read of the submitted version. Advisory, so it must never fail the submit.
    try:
        await sentiment_service.request_analysis(submission_id, user_id, trigger="SUBMIT")
    except Exception:
        logger.warning("Could not start sentiment analysis for %s", submission_id, exc_info=True)

    return await get_submission(submission_id, user_id), _review_job(dict(job))


async def withdraw(submission_id: UUID, user_id: UUID) -> Submission:
    """Pull an in-review submission back to draft so the creator can edit and resubmit."""
    row = await _get_owned_row(submission_id, user_id)
    if row["status"] != "IN_REVIEW":
        raise HTTPException(status_code=409, detail="Only submissions that are in review can be withdrawn")
    pool = get_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            await conn.execute(
                """
                UPDATE content_review_jobs SET status = 'WITHDRAWN', completed_at = NOW()
                WHERE submission_id = $1 AND status = 'AWAITING_REVIEW'
                """,
                submission_id,
            )
            await conn.execute(
                "UPDATE content_submissions SET status = 'DRAFT', updated_at = NOW(), updated_by = $2 WHERE id = $1",
                submission_id, user_id,
            )
            await _add_event(conn, submission_id, "WITHDRAWN",
                             f"Version {row['current_version']} withdrawn from review for editing", user_id)
    await log_audit_event("content_submission", str(submission_id), "WITHDRAW_SUBMISSION", user_id,
                          {"reference_code": row["reference_code"], "version": row["current_version"]})
    return await get_submission(submission_id, user_id)


async def review_queue(user_id: UUID) -> List[ReviewQueueItem]:
    """Submitted videos for the Content Screener: everyone's for super admins, otherwise the user's own."""
    see_all = await _is_super_admin(user_id)
    rows = await fetch_all(
        """
        SELECT s.id, s.reference_code, s.title, s.status, s.platforms, s.caption_text, s.latest_check,
               s.current_version, s.submitted_at, u.full_name AS creator_name,
               c.id AS acc_id, c.provider AS acc_provider, c.account_name AS acc_account_name,
               c.connection_metadata AS acc_connection_metadata,
               f.id AS file_id, f.kind AS file_kind, f.original_name AS file_original_name,
               f.mime_type AS file_mime_type, f.size_bytes AS file_size_bytes, f.created_at AS file_created_at,
               f.transcript AS file_transcript, f.transcript_status AS file_transcript_status,
               f.transcript_error AS file_transcript_error
        FROM content_submissions s
        JOIN LATERAL (
            SELECT * FROM content_submission_files cf
            WHERE cf.submission_id = s.id AND cf.kind = 'MAIN' AND cf.deleted_at IS NULL
              AND cf.mime_type LIKE 'video/%'
            ORDER BY cf.created_at DESC
            LIMIT 1
        ) f ON true
        LEFT JOIN oauth_connections c ON c.id = s.account_id
        LEFT JOIN users u ON u.id = s.created_by
        WHERE s.status IN ('IN_REVIEW', 'CHANGES_REQUESTED', 'APPROVED', 'REJECTED')
          AND ($2 OR s.created_by = $1)
        ORDER BY s.submitted_at DESC NULLS LAST
        LIMIT 100
        """,
        user_id,
        see_all,
    )
    # Videos uploaded before transcription existed (or whose attempt was abandoned) get transcribed now.
    await transcription_service.schedule_missing(
        [r["file_id"] for r in rows if r["file_transcript_status"] not in ("COMPLETED", "FAILED")]
    )
    return [
        ReviewQueueItem(
            id=r["id"],
            reference_code=r["reference_code"],
            title=r["title"],
            status=r["status"],
            platforms=list(r["platforms"] or []),
            account=_account_option(r, prefix="acc_"),
            caption_text=r["caption_text"],
            latest_check=_json(r["latest_check"]),
            current_version=r["current_version"],
            submitted_at=r["submitted_at"],
            creator_name=r["creator_name"],
            video=_file_out(r["id"], {
                "id": r["file_id"], "kind": r["file_kind"], "original_name": r["file_original_name"],
                "mime_type": r["file_mime_type"], "size_bytes": r["file_size_bytes"], "created_at": r["file_created_at"],
                "transcript": r["file_transcript"], "transcript_error": r["file_transcript_error"],
                # schedule_missing may have just queued it; report that rather than "never transcribed".
                "transcript_status": r["file_transcript_status"] or "PENDING",
            }),
        )
        for r in rows
    ]


async def get_history(submission_id: UUID, user_id: UUID) -> SubmissionHistory:
    await _get_owned_row(submission_id, user_id)
    versions = await fetch_all(
        """
        SELECT v.version_number, v.snapshot, v.created_at, u.full_name AS created_by_name
        FROM content_submission_versions v LEFT JOIN users u ON u.id = v.created_by
        WHERE v.submission_id = $1 ORDER BY v.version_number DESC
        """,
        submission_id,
    )
    jobs = await fetch_all(
        "SELECT * FROM content_review_jobs WHERE submission_id = $1 ORDER BY created_at DESC",
        submission_id,
    )
    events = await fetch_all(
        """
        SELECT e.id, e.event_type, e.message, e.data, e.created_at, u.full_name AS actor_name
        FROM content_submission_events e LEFT JOIN users u ON u.id = e.actor_user_id
        WHERE e.submission_id = $1 ORDER BY e.created_at DESC, e.id DESC
        """,
        submission_id,
    )
    return SubmissionHistory(
        versions=[SubmissionVersion(version_number=v["version_number"], snapshot=_json(v["snapshot"]) or {},
                                    created_by_name=v["created_by_name"], created_at=v["created_at"]) for v in versions],
        review_jobs=[_review_job(j) for j in jobs],
        events=[SubmissionEvent(id=e["id"], event_type=e["event_type"], message=e["message"], data=_json(e["data"]),
                                actor_name=e["actor_name"], created_at=e["created_at"]) for e in events],
    )
