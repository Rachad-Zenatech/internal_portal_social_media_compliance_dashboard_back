"""Content Submission Service
Drafts, file storage, versioning, review jobs and activity history for content submissions.
"""

import asyncio
import json
import time
import logging
import mimetypes
import os
import re
import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple
from uuid import UUID, uuid4

import aiofiles
from fastapi import HTTPException, UploadFile, status

from models.compliance.compliance_model import ComplianceCheckRequest, ProvidedSource
from models.notifications.notification_model import NotificationCreate
from models.compliance.submission_model import (
    DashboardActivity,
    DashboardFinding,
    DashboardSummary,
    ReviewComment,
    ReviewCommentCreate,
    ReviewDecisionRequest,
    ReopenRequest,
    RescheduleRequest,
    AUDIENCES,
    PLATFORMS,
    PROVIDER_PLATFORMS,
    REGIONS,
    REVIEWER_ROUTES,
    RULE_SETS,
    AccountOption,
    BrandOption,
    ReviewJob,
    ReviewerOption,
    VersionView,
    ReviewQueueCounts,
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
from services.shared.core.audit_service import log_audit_event
from services.ai.observability import ai_observability, ai_usage_service
from services.compliance.submissions import approval_path_service
from services.compliance.submissions import delegation_service
from services.compliance.submissions import duplicate_service
from services.compliance.media import canva_service
from services.compliance.submissions import flag_review_service
from services.compliance.submissions import review_deadline_service
from services.compliance.media import transcription_service
from services.compliance.submissions import analysis_progress_service
from services.compliance.submissions import post_steps_service
from services.compliance.media import document_layout
from services.compliance.media import document_text_service
from services.compliance.media import image_text_service
from services.compliance.submissions import review_memory_service
from services.compliance.evaluation import script_match
from services.ai.observability.ai_usage_service import AiLimitExceeded
from services.compliance.evaluation.compliance_service import CEO_BRAND, audiences_of, brands_of, check_compliance, flag_segments
from services.compliance.evaluation.compliance_service import parts_for as compliance_parts_for
from services.compliance.evaluation.compliance_service import rulebook_version as compliance_rulebook_version
from services.compliance.media.html_sanitizer import sanitize_caption_html

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
    "SCRIPT": {".pdf", ".doc", ".docx", ".txt", ".md"},
    "IMAGE": IMAGE_EXT,
    "SUPPORTING": DOC_EXT | {".xls", ".xlsx", ".csv", ".zip", ".ppt", ".pptx"} | IMAGE_EXT,
}
MAIN_EXT_BY_TYPE = {
    "VIDEO": VIDEO_EXT,
    "IMAGE": IMAGE_EXT,
    "CAROUSEL": IMAGE_EXT,
    # Images and text only: a video goes in a Video post, which has the script step and the video review.
    "SOCIAL_POST": IMAGE_EXT,
    "BLOG": DOC_EXT,
    "AD": IMAGE_EXT | VIDEO_EXT,
}
# Types whose main asset is required to submit; posts and articles can be text-only. An ad needs its creative.
MAIN_FILE_REQUIRED = {"VIDEO", "IMAGE", "CAROUSEL", "AD"}
# Saved fields that don't change what the compliance check, sentiment analysis or public mood read.
_NOT_CONTENT_FIELDS = {"approval_needed_by", "reviewer_ids", "reviewer_route"}


def _same_value(new: Any, old: Any) -> bool:
    """Whether a saved value is what the post already holds (a list in another order, or a date as text, still counts)."""
    if isinstance(new, list) and isinstance(old, list):
        return [str(x) for x in new] == [str(x) for x in old]
    if isinstance(old, datetime) and isinstance(new, str):
        try:
            parsed = datetime.fromisoformat(new.replace("Z", "+00:00"))
        except ValueError:
            return False
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=old.tzinfo)
        return parsed == old
    if isinstance(new, UUID) or isinstance(old, UUID):
        return str(new) == str(old)
    return new == old


def content_changed(row: Dict[str, Any], fields: Dict[str, Any]) -> bool:
    """Whether saving these fields changes anything the checks read. A save that only repeats what is already there
    (every save from the editor sends the whole form) leaves the checks up to date."""
    return any(c not in _NOT_CONTENT_FIELDS and not _same_value(v, row.get(c)) for c, v in fields.items())


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


# Every account a submission posts through, primary first, as one JSON column of the same SELECT
# (needs content_submissions aliased as s).
_ACCOUNTS_COLUMN = """
       (SELECT coalesce(jsonb_agg(jsonb_build_object(
                   'id', ac.id, 'provider', ac.provider, 'account_name', ac.account_name,
                   'connection_metadata', ac.connection_metadata)
                 ORDER BY array_position(s.account_ids, ac.id)), '[]'::jsonb)
          FROM oauth_connections ac WHERE ac.id = ANY(s.account_ids)) AS accounts_json"""


def _accounts(row: Dict[str, Any]) -> List[AccountOption]:
    items = row.get("accounts_json")
    if isinstance(items, str):
        items = json.loads(items)
    return [a for a in (_account_option(item) for item in items or []) if a]


def _account_ids(row: Dict[str, Any]) -> List[UUID]:
    return list(row.get("account_ids") or ([row["account_id"]] if row.get("account_id") else []))


def _account_covers(account: AccountOption, platforms: List[str]) -> bool:
    """Whether an account publishes to one of the platforms. YouTube accounts post both videos and Shorts."""
    return account.platform in platforms or (account.platform == "YOUTUBE" and "YOUTUBE_SHORTS" in platforms)


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
        doc_text=row.get("doc_text"),
        doc_text_status=row.get("doc_text_status"),
        doc_text_error=row.get("doc_text_error"),
        source=row.get("source") or "UPLOAD",
        canva_design_id=row.get("canva_design_id"),
        canva_design_title=row.get("canva_design_title"),
        canva_revision=row.get("canva_revision"),
    )


async def _is_super_admin(user_id: UUID) -> bool:
    row = await fetch_one("SELECT is_super_admin FROM users WHERE id = $1", user_id)
    return bool(row and row["is_super_admin"])


async def _can_manage_team_content(user_id: UUID) -> bool:
    """Super admins and roles with "Manage Team Drafts" (Content Manager, Administrator) see everyone's content
    and can delete other people's drafts."""
    from services.shared.auth.auth_service import user_can_access_page
    return await _is_super_admin(user_id) or await user_can_access_page(user_id, "CONTENT_SUBMISSIONS", "DELETE")


async def _can_review(user_id: UUID) -> bool:
    """Reviewers (the people who publish): "Review & Publish Content" (Content Reviewer, Content Manager,
    Administrator) and super admins."""
    from services.shared.auth.auth_service import user_can_access_page
    return await _is_super_admin(user_id) or await user_can_access_page(user_id, "CONTENT_REVIEW", "APPROVE")


async def _can_see_team_content(user_id: UUID) -> bool:
    """Content Managers and reviewers can read everyone's submissions."""
    return await _can_manage_team_content(user_id) or await _can_review(user_id)


async def _brand_scope(user_id: UUID) -> Optional[List[str]]:
    """The brand codes the user works on, or None when they work on every brand: super admins, and anyone who
    hasn't been assigned to specific brands. Other people's content outside these brands is hidden from them."""
    row = await fetch_one(
        """
        SELECT u.is_super_admin,
               coalesce(array_agg(ub.brand_code) FILTER (WHERE ub.brand_code IS NOT NULL), '{}') AS brands
        FROM users u LEFT JOIN user_brands ub ON ub.user_id = u.id
        WHERE u.id = $1
        GROUP BY u.id
        """,
        user_id,
    )
    if not row or row["is_super_admin"] or not row["brands"]:
        return None
    return list(row["brands"])


def _sent_to(user_param: str = "$1") -> str:
    """SQL: the content was sent to this user (picked, or delegated to them), to someone they outrank in the reviewer
    hierarchy (see content_review_rank), or to no one in particular (every reviewer on its brand)."""
    return (
        f"(cardinality(s.reviewer_ids) = 0 OR {user_param} = ANY(s.reviewer_ids)"
        f" OR content_review_rank({user_param}) > (SELECT min(content_review_rank(x)) FROM unnest(s.reviewer_ids) x))"
    )


async def _outranks(user_id: UUID, reviewer_ids: List[UUID]) -> bool:
    """The user is above at least one of these reviewers in the reviewer hierarchy."""
    row = await fetch_one(
        "SELECT content_review_rank($1) > (SELECT min(content_review_rank(x)) FROM unnest($2::uuid[]) x) AS above",
        user_id, list(reviewer_ids),
    )
    return bool(row and row["above"])


async def _in_review_scope(user_id: UUID, reviewer_ids: List[UUID]) -> bool:
    """Python twin of _sent_to for one loaded row."""
    if not reviewer_ids or user_id in reviewer_ids:
        return True
    return await _outranks(user_id, reviewer_ids)


async def _get_owned_row(submission_id: UUID, user_id: UUID, manager_can_read: bool = False) -> Dict[str, Any]:
    """Load a submission the user may access (creator or super admin; managers and reviewers too when
    manager_can_read, for the brands they work on), with its account joined. Deleted submissions are never returned."""
    row = await fetch_one(
        """
        SELECT s.*,
               c.id AS acc_id, c.provider AS acc_provider, c.account_name AS acc_account_name,
               c.connection_metadata AS acc_connection_metadata,"""
        + _ACCOUNTS_COLUMN
        + """
        FROM content_submissions s
        LEFT JOIN oauth_connections c ON c.id = s.account_id
        WHERE s.id = $1 AND s.deleted_at IS NULL
        """,
        submission_id,
    )
    if not row:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Submission not found")
    if row["created_by"] != user_id and not await _is_super_admin(user_id):
        if not (manager_can_read and await _can_see_team_content(user_id)):
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Submission not found")
        scope = await _brand_scope(user_id)
        if scope is not None and not set(brands_of(row)) & set(scope):
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Submission not found")
        # Sent to particular reviewers: only they, their delegates and those above them in the reviewer hierarchy
        # (besides its creator and super admins) may open it.
        if not await _in_review_scope(user_id, list(row.get("reviewer_ids") or [])):
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Submission not found")
    return row


def _approvers(rules: "approval_path_service.ApprovalRules", row: Dict[str, Any], accounts=None) -> Tuple[List[UUID], List[str]]:
    """The named final approvers for a submission row (empty: any reviewer on its brand)."""
    accts = accounts if accounts is not None else [_account_option(row, prefix="acc_")]
    ceo = approval_path_service.is_ceo_content(row.get("rule_sets") or [], accts)
    return rules.approvers(brands_of(row), audiences_of(row), ceo)


def _may_approve(user_id: UUID, is_super: bool, approver_ids: List[UUID]) -> bool:
    return is_super or not approver_ids or user_id in approver_ids


def _approval_fields(rules, r: Dict[str, Any], user_id: UUID, is_reviewer: bool, is_super: bool, accounts=None,
                     edges=None) -> Dict[str, Any]:
    ids, names = _approvers(rules, r, accounts)
    # A named final approver who delegated the post passes their approval to the delegate.
    ids = delegation_service.extend(ids, edges or [])
    can_review = r["status"] == "IN_REVIEW" and is_reviewer and (r["created_by"] != user_id or is_super)
    return {"approver_names": names, "can_approve": can_review and _may_approve(user_id, is_super, ids),
            "can_delegate": can_review}


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
               transcript, transcript_status, transcript_error, doc_text, doc_text_status, doc_text_error,
               source, canva_design_id, canva_design_title, canva_revision
        FROM content_submission_files
        WHERE submission_id = $1 AND deleted_at IS NULL
        ORDER BY created_at ASC
        """,
        submission_id,
    )


def _due(row: Dict[str, Any]) -> Dict[str, Any]:
    """The open deadline, if the content is still at the step it belongs to."""
    kind = row.get("due_kind")
    if row.get("due_at") and review_deadline_service.KIND_STATUS.get(kind) == row["status"]:
        return {"due_at": row["due_at"], "due_kind": kind}
    # A video post's script: sent back with a deadline for the changes, or approved with a deadline for the video.
    script = row.get("script_status")
    if row.get("due_at") and row["status"] == "DRAFT" and (
        (kind == "REVISION" and script in ("CHANGES_REQUESTED", "EDITS_PROPOSED")) or (kind == "VIDEO" and script == "APPROVED")
    ):
        return {"due_at": row["due_at"], "due_kind": kind}
    return {"due_at": None, "due_kind": None}


async def _to_submission(row: Dict[str, Any]) -> Submission:
    files = await _list_files(row["id"])
    return Submission(
        id=row["id"],
        reference_code=row["reference_code"],
        title=row["title"],
        content_type=row["content_type"],
        brand_code=row["brand_code"],
        brand_codes=brands_of(row),
        audience=row["audience"],
        audiences=audiences_of(row),
        region=row["region"],
        platforms=list(row["platforms"] or []),
        account_id=row["account_id"],
        account=_account_option(row, prefix="acc_"),
        account_ids=_account_ids(row),
        accounts=_accounts(row),
        publish_at=row["publish_at"],
        approval_needed_by=row.get("approval_needed_by"),
        **_due(row),
        caption_html=row["caption_html"],
        caption_text=row["caption_text"],
        rule_sets=list(row["rule_sets"] or []),
        reviewer_route=list(row["reviewer_route"] or []),
        reviewer_ids=list(row.get("reviewer_ids") or []),
        status=row["status"],
        current_version=row["current_version"],
        latest_check=_json(row["latest_check"]),
        files=[_file_out(row["id"], f) for f in files],
        can_edit=row["status"] in EDITABLE_STATUSES,
        script_required=script_stage_on(row),
        script_status=(row.get("script_status") or "DRAFT") if script_stage_on(row) else None,
        submitted_at=row["submitted_at"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


async def _connected_accounts(account_ids: List[UUID]) -> List[AccountOption]:
    """Those of the given publishing accounts that are connected and not expired, in the order given."""
    if not account_ids:
        return []
    rows = await fetch_all(
        """
        SELECT id, provider, account_name, connection_metadata
        FROM oauth_connections
        WHERE id = ANY($1::uuid[]) AND status = 'CONNECTED' AND (expires_at IS NULL OR expires_at > NOW())
        """,
        list(account_ids),
    )
    by_id = {r["id"]: r for r in rows if r["provider"].upper() in PROVIDER_PLATFORMS}
    return [_account_option(by_id[i]) for i in account_ids if i in by_id]


# ---------------------------------------------------------------------------
# Options
# ---------------------------------------------------------------------------

async def get_options(user_id: UUID) -> SubmissionOptions:
    """Choices for the submission form, limited to the brands the user works on (and those brands' accounts)."""
    scope = await _brand_scope(user_id)
    brands = await fetch_all(
        "SELECT code, name FROM brands WHERE is_active AND ($1::text[] IS NULL OR code = ANY($1::text[])) ORDER BY sort_order, name",
        scope,
    )
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
        # Accounts not tied to a brand stay available to everyone.
        accounts=[
            a for a in (_account_option(r) for r in accounts)
            if a and (scope is None or not a.brand_code or a.brand_code in scope)
        ],
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
                INSERT INTO content_submissions (title, content_type, created_by, updated_by, script_required)
                VALUES ($1, $2::varchar, $3, $3, $2::varchar = 'VIDEO')
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


async def list_submissions(user_id: UUID, status_filter: Optional[str] = None, scope: str = "mine") -> List[SubmissionSummary]:
    """The user's own submissions, or everyone's (scope="all") for super admins, Content Managers and reviewers."""
    is_manager = await _can_manage_team_content(user_id)
    is_reviewer = await _can_review(user_id)
    is_super = await _is_super_admin(user_id)
    if scope == "all" and not (is_manager or is_reviewer):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="You can only see your own content")
    rows = await fetch_all(
        """
        SELECT s.id, s.reference_code, s.title, s.content_type, s.brand_code, s.brand_codes, s.platforms, s.status,
               s.current_version, s.publish_at, s.approval_needed_by, s.submitted_at, s.created_at, s.updated_at, s.audience, s.audiences,
               s.due_at, s.due_kind, s.rule_sets, s.region, s.script_required,
               -- What the five steps are worked out from: when each tool last ran, and when the content last changed.
               s.content_edited_at,
               (s.latest_check->>'checked_at')::timestamptz AS check_at,
               (SELECT max(a.completed_at) FROM content_sentiment_analyses a
                 WHERE a.submission_id = s.id AND a.status = 'COMPLETED') AS sentiment_at,
               EXISTS (SELECT 1 FROM content_sentiment_analyses a WHERE a.submission_id = s.id
                        AND a.status IN ('WAITING_TRANSCRIPT', 'PENDING', 'PROCESSING')) AS sentiment_running,
               (SELECT max(m.created_at) FROM public_mood_summaries m WHERE m.submission_id = s.id) AS mood_at,
               EXISTS (SELECT 1 FROM content_submission_files mf WHERE mf.submission_id = s.id AND mf.kind = 'MAIN'
                        AND mf.deleted_at IS NULL) AS has_main,
               EXISTS (SELECT 1 FROM content_submission_files mf WHERE mf.submission_id = s.id AND mf.kind = 'MAIN'
                        AND mf.deleted_at IS NULL AND mf.mime_type LIKE 'video/%') AS main_is_video,
               CASE WHEN s.script_required AND s.content_type = 'VIDEO' THEN coalesce(s.script_status, 'DRAFT') END
                   AS script_status,
               CASE WHEN s.script_send->>'status' = 'PROCESSING'
                         AND (s.script_send->>'started_at')::timestamptz < NOW() - INTERVAL '10 minutes' THEN 'FAILED'
                    ELSE s.script_send->>'status' END AS script_send_status,
               CASE WHEN s.script_send->>'status' = 'PROCESSING'
                         AND (s.script_send->>'started_at')::timestamptz < NOW() - INTERVAL '10 minutes'
                    THEN 'Sending the script took too long. Send it again.' ELSE s.script_send->>'error' END AS script_send_error,
               left(s.caption_text, 600) AS caption_text,
               -- The script as the card's preview until a video is added: the uploaded file's text, else the last version sent.
               CASE WHEN s.script_required AND s.content_type = 'VIDEO' THEN coalesce(
                   (SELECT left(sf.doc_text, 500) FROM content_submission_files sf
                     WHERE sf.submission_id = s.id AND sf.kind = 'SCRIPT' AND sf.deleted_at IS NULL
                       AND sf.doc_text_status = 'COMPLETED' AND sf.doc_text <> ''
                     ORDER BY sf.created_at DESC LIMIT 1),
                   (SELECT left(sv.body, 500) FROM content_script_versions sv
                     WHERE sv.submission_id = s.id ORDER BY sv.version_number DESC LIMIT 1)
               ) END AS script_excerpt,
               s.latest_check->>'traffic_light' AS latest_traffic_light,
               CASE WHEN """ + _pending_sql("s.latest_check") + " OR " + _unavailable_sql("s.latest_check") + """ THEN NULL ELSE (s.latest_check->>'score')::int END AS latest_score,
               CASE WHEN """ + _pending_sql("s.latest_check") + " OR " + _unavailable_sql("s.latest_check") + """ THEN NULL
                    ELSE COALESCE((s.latest_check->>'violations_count')::int,
                                  CASE WHEN jsonb_typeof(s.latest_check->'violations') = 'array'
                                       THEN jsonb_array_length(s.latest_check->'violations') END)
               END AS issue_count,
               """ + _pending_sql("s.latest_check") + """ AS analysis_pending,
               """ + _unavailable_sql("s.latest_check") + """ AS ai_unavailable,
               (SELECT count(*) FROM content_submission_files f
                 WHERE f.submission_id = s.id AND f.deleted_at IS NULL) AS file_count,
               (SELECT count(*) FROM content_review_comments rc
                 WHERE rc.submission_id = s.id AND rc.deleted_at IS NULL) AS comment_count,
               c.id AS acc_id, c.provider AS acc_provider, c.account_name AS acc_account_name,
               c.connection_metadata AS acc_connection_metadata,
               cov.id AS cov_id, cov.kind AS cov_kind, cov.original_name AS cov_original_name,
               cov.mime_type AS cov_mime_type, cov.size_bytes AS cov_size_bytes, cov.created_at AS cov_created_at,
               s.created_by, u.full_name AS created_by_name,"""
        + _ACCOUNTS_COLUMN
        + """
        FROM content_submissions s
        LEFT JOIN users u ON u.id = s.created_by
        LEFT JOIN oauth_connections c ON c.id = s.account_id
        -- Thumbnail: the main video/image, otherwise the first attached image.
        LEFT JOIN LATERAL (
            SELECT * FROM content_submission_files cf
            WHERE cf.submission_id = s.id AND cf.deleted_at IS NULL
              AND (cf.mime_type LIKE 'video/%' OR cf.mime_type LIKE 'image/%')
            ORDER BY (cf.kind = 'MAIN') DESC, cf.created_at ASC
            LIMIT 1
        ) cov ON true
        WHERE (s.created_by = $1 OR ($3 AND ($4::text[] IS NULL OR s.brand_codes && $4::text[])
                                     AND """ + _sent_to() + """))
          AND s.deleted_at IS NULL AND ($2::text IS NULL OR s.status = $2)
        -- What needs the creator comes first: changes to fix (a post's or its script's), then drafts to finish
        -- (including an approved script waiting for its video). Then what's waiting on someone else: content in
        -- review, and a script with a reviewer or being sent, which count the same however they're stored (a script
        -- in review is a draft underneath). Finished content goes last. Within each group the soonest deadline
        -- (its due date, else the date approval is needed by, else the publish date) is first.
        ORDER BY CASE
                     WHEN s.status = 'CHANGES_REQUESTED' OR s.script_status IN ('CHANGES_REQUESTED', 'EDITS_PROPOSED') THEN 0
                     WHEN s.status = 'DRAFT' AND coalesce(s.script_status, '') <> 'IN_REVIEW'
                          AND coalesce(s.script_send->>'status', '') <> 'PROCESSING' THEN 1
                     WHEN s.status = 'IN_REVIEW' OR s.script_status = 'IN_REVIEW'
                          OR s.script_send->>'status' = 'PROCESSING' THEN 2
                     ELSE 3
                 END,
                 coalesce(s.due_at, s.approval_needed_by, s.publish_at) ASC NULLS LAST,
                 s.updated_at DESC
        LIMIT 200
        """,
        user_id,
        status_filter,
        scope == "all",
        # Everyone else's content only for the brands the viewer works on.
        await _brand_scope(user_id),
    )
    rules = await approval_path_service.load_rules()
    summaries = []
    edges = await delegation_service.edges_for([r["id"] for r in rows if r["status"] == "IN_REVIEW"])
    for r in rows:
        base = {k: v for k, v in r.items()
                if not k.startswith(("acc_", "cov_"))
                and k not in ("platforms", "accounts_json", "due_at", "due_kind", "rule_sets")}
        cover = {k[4:]: v for k, v in r.items() if k.startswith("cov_")}
        if base.get("script_excerpt"):
            # The card shows the wording, without the page, heading and table marks.
            base["script_excerpt"] = " ".join(document_layout.plain_text(base["script_excerpt"]).split())
        summaries.append(SubmissionSummary(
            **base,
            platforms=list(r["platforms"] or []),
            **_due(r),
            account=_account_option(r, prefix="acc_"),
            accounts=_accounts(r),
            cover=_file_out(r["id"], cover) if cover["id"] else None,
            can_delete=_can_delete(r, r["created_by"] == user_id or is_manager),
            can_review=r["status"] == "IN_REVIEW" and is_reviewer and (r["created_by"] != user_id or is_super),
            **_approval_fields(rules, r, user_id, is_reviewer, is_super, _accounts(r), edges.get(r["id"])),
            can_manage_approval=r["status"] == "APPROVED" and is_reviewer and (r["created_by"] != user_id or is_super),
            steps=post_steps_service.compute_steps({
                **r,
                "has_account": bool(_accounts(r)),
                "main_required": r["content_type"] in MAIN_FILE_REQUIRED,
                "script_stage": script_stage_on(r),
                "latest_score": r["latest_score"],
            }),
        ))
    # Posts still being read: what's left and when their score should be ready.
    progress = await analysis_progress_service.for_submissions([s.id for s in summaries if s.analysis_pending])
    for s in summaries:
        s.analysis_progress = progress.get(s.id)
    return summaries


async def get_submission(submission_id: UUID, user_id: UUID) -> Submission:
    row = await _get_owned_row(submission_id, user_id, manager_can_read=True)
    sub = await _to_submission(row)
    is_creator = row["created_by"] == user_id
    # Managers can read and delete someone else's draft but not edit it.
    sub.can_edit = sub.can_edit and (is_creator or await _is_super_admin(user_id))
    sub.can_delete = _can_delete(row, is_creator or await _can_manage_team_content(user_id))
    approver_ids, sub.approver_names = _approvers(await approval_path_service.load_rules(), row, _accounts(row))
    if row["status"] in ("IN_REVIEW", "APPROVED"):
        is_super = await _is_super_admin(user_id)
        may_decide = await _can_review(user_id) and (not is_creator or is_super)
        sub.can_review = row["status"] == "IN_REVIEW" and may_decide
        sub.can_delegate = sub.can_review
        approver_ids = await delegation_service.with_delegates(row["id"], approver_ids)
        sub.can_approve = sub.can_review and _may_approve(user_id, is_super, approver_ids)
        sub.can_manage_approval = row["status"] == "APPROVED" and may_decide
    standing = (await flag_review_service.approval_standing([row])).get(row["id"])
    if standing:
        sub.open_critical_flags, sub.approval_score = standing.open_critical, standing.score
        sub.min_approval_score = standing.min_score
    saved = _json(row["latest_check"]) or {}
    if saved:
        current = await compliance_rulebook_version()
        sub.check_outdated = bool(current) and saved.get("rulebook_version") != current
    if analysis_pending(saved):
        sub.analysis_progress = (await analysis_progress_service.for_submissions([row["id"]])).get(row["id"])
    sub.created_by = row["created_by"]
    if not is_creator:
        creator = await fetch_one("SELECT full_name FROM users WHERE id = $1", row["created_by"])
        sub.created_by_name = creator["full_name"] if creator else None
    return sub


async def update_submission(submission_id: UUID, data: SubmissionUpdate, user_id: UUID) -> Submission:
    row = await _get_owned_row(submission_id, user_id)
    _require_editable(row)

    fields: Dict[str, Any] = {}
    payload = data.model_dump(exclude_unset=True, exclude={"clear_fields"})

    if "title" in payload and payload["title"] is not None:
        fields["title"] = payload["title"].strip()
    if payload.get("content_type"):
        fields["content_type"] = payload["content_type"]
        # A draft switched to a video before it was ever sent goes through the script stage too.
        if payload["content_type"] == "VIDEO" and row["current_version"] == 0 and not row.get("script_required"):
            fields["script_required"] = True
    brand_codes = payload.get("brand_codes")
    if brand_codes is None and payload.get("brand_code"):
        brand_codes = [payload["brand_code"]]
    if brand_codes is not None:
        brand_codes = list(dict.fromkeys(brand_codes))
        found = await fetch_all("SELECT code FROM brands WHERE code = ANY($1::text[]) AND is_active", brand_codes)
        if len(found) != len(brand_codes):
            raise HTTPException(status_code=422, detail="Unknown brand")
        # Only checked for brands being added, so an older draft on a brand someone has since left still saves.
        added = [b for b in brand_codes if b not in brands_of(row)]
        scope = await _brand_scope(user_id)
        if added and scope is not None and any(b not in scope for b in added):
            raise HTTPException(status_code=403, detail="You don't work on that brand")
        fields["brand_codes"] = brand_codes
        fields["brand_code"] = brand_codes[0] if brand_codes else None
    audiences = payload.get("audiences")
    if audiences is None and payload.get("audience"):
        audiences = [payload["audience"]]
    if audiences is not None:
        audiences = list(dict.fromkeys(audiences))
        if any(a not in AUDIENCES for a in audiences):
            raise HTTPException(status_code=422, detail="Unknown target audience")
        fields["audiences"] = audiences
        fields["audience"] = audiences[0] if audiences else None
    if payload.get("region"):
        if payload["region"] not in REGIONS:
            raise HTTPException(status_code=422, detail="Unknown region")
        fields["region"] = payload["region"]
    if payload.get("platforms") is not None:
        bad = [p for p in payload["platforms"] if p not in PLATFORMS]
        if bad:
            raise HTTPException(status_code=422, detail=f"Unknown platform(s): {', '.join(bad)}")
        fields["platforms"] = list(dict.fromkeys(payload["platforms"]))
    account_ids = payload.get("account_ids")
    if account_ids is None and payload.get("account_id"):
        account_ids = [payload["account_id"]]
    if account_ids is not None:
        account_ids = list(dict.fromkeys(account_ids))
        if len(await _connected_accounts(account_ids)) != len(account_ids):
            raise HTTPException(status_code=422, detail="One of those publishing accounts isn't connected")
        fields["account_ids"] = account_ids
        fields["account_id"] = account_ids[0] if account_ids else None
    if payload.get("publish_at"):
        fields["publish_at"] = payload["publish_at"]
    # Saved as picked; whether it's still in the future (and before the publish date) is checked on submit, so
    # autosave never fails because time passed.
    if payload.get("approval_needed_by"):
        fields["approval_needed_by"] = payload["approval_needed_by"]
    if "caption_html" in payload:
        # Shown back to other people in the editor, so only safe formatting is kept.
        fields["caption_html"] = sanitize_caption_html(payload["caption_html"])
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
    if payload.get("reviewer_ids") is not None:
        picked = list(dict.fromkeys(payload["reviewer_ids"]))
        allowed = set(await _reviewer_ids(exclude=await _creator_exclusion(row["created_by"]),
                                          brand_codes=fields.get("brand_codes", brands_of(row))))
        if any(r not in allowed for r in picked):
            raise HTTPException(status_code=422, detail="Some of those people can't review content on this brand")
        fields["reviewer_ids"] = picked
    for name in data.clear_fields:
        fields[name] = None
        if name == "account_id":
            fields["account_ids"] = []
        if name == "audience":
            fields["audiences"] = []
        if name == "brand_code":
            fields["brand_codes"] = []

    if not fields:
        return await _to_submission(row)

    columns = list(fields.keys())
    sets = ", ".join(f"{c} = ${i + 2}" for i, c in enumerate(columns))
    params = [submission_id, *fields.values(), user_id]
    # Changing anything the checks read makes their results out of date; who reviews it and by when does not.
    edited = ", content_edited_at = NOW()" if content_changed(row, fields) else ""
    pool = get_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            await conn.execute(
                f"UPDATE content_submissions SET {sets}{edited}, updated_by = ${len(params)}, updated_at = NOW() WHERE id = $1",
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


def _script_with_reviewer(row: Dict[str, Any]) -> bool:
    """A video post's script is with a reviewer, or being sent to them: the post is still a draft, but it has been sent
    for review, so it can't be deleted from under them."""
    if row.get("script_status") == "IN_REVIEW" or row.get("script_send_status") == "PROCESSING":
        return True
    if row.get("script_send"):
        from services.compliance.evaluation import script_review_service

        return script_review_service._send_state(row)["status"] == "PROCESSING"
    return False


def _can_delete(row: Dict[str, Any], allowed: bool) -> bool:
    """Only a draft that hasn't gone to a reviewer: not in review, not approved, and no script out with a reviewer."""
    return allowed and row["status"] == "DRAFT" and not _script_with_reviewer(row)


async def delete_draft(submission_id: UUID, user_id: UUID) -> None:
    """Delete a draft. Allowed for its creator, super admins and Content Managers.
    A draft that was never submitted is removed with its files; one withdrawn after review is hidden instead,
    so its versions, review jobs and activity stay on record."""
    row = await _get_owned_row(submission_id, user_id, manager_can_read=True)
    if row["status"] != "DRAFT":
        raise HTTPException(status_code=409, detail="Only drafts can be deleted. Withdraw it from review first.")
    if _script_with_reviewer(row):
        raise HTTPException(status_code=409, detail="Its script is with a reviewer, so it can't be deleted until they reply.")
    is_creator = row["created_by"] == user_id
    if not is_creator and not await _can_manage_team_content(user_id):
        raise HTTPException(status_code=403, detail="Only the person who created this draft, a manager or a super admin can delete it")

    pool = get_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            # Lock and re-check, so a draft submitted a moment ago isn't deleted from under its review.
            current = await conn.fetchval(
                "SELECT status FROM content_submissions WHERE id = $1 AND deleted_at IS NULL FOR UPDATE", submission_id
            )
            if current != "DRAFT":
                raise HTTPException(status_code=409, detail="This draft was just submitted or deleted")
            if row["current_version"] == 0:
                await conn.execute("DELETE FROM content_submissions WHERE id = $1", submission_id)
            else:
                await conn.execute(
                    "UPDATE content_submissions SET deleted_at = NOW(), deleted_by = $2, updated_at = NOW() WHERE id = $1",
                    submission_id, user_id,
                )
                await _add_event(conn, submission_id, "DELETED", "Draft deleted", user_id, None)
    if row["current_version"] == 0:
        # Never-submitted drafts have no version history that could reference these files.
        shutil.rmtree(STORAGE_ROOT / str(submission_id), ignore_errors=True)
    await log_audit_event("content_submission", str(submission_id), "DELETE_SUBMISSION_DRAFT", user_id,
                          {"reference_code": row["reference_code"], "title": row["title"],
                           "created_by": str(row["created_by"]), "kept_history": row["current_version"] > 0})

    if not is_creator:
        # Someone else removed their work: tell the creator who and what.
        try:
            from services.notifications.notification_service import create_notification
            actor = await fetch_one("SELECT full_name FROM users WHERE id = $1", user_id)
            await create_notification(NotificationCreate(
                user_id=str(row["created_by"]),
                type="SUBMISSION",
                title=f"{row['reference_code']} draft was deleted",
                message=f"\"{row['title']}\" was deleted by {actor['full_name'] if actor else 'a manager'}.",
                entity_type="content_submission",
                entity_id=str(submission_id),
            ))
        except Exception:
            logger.warning("Could not notify the creator about a deleted draft", exc_info=True)


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
    _require_script_stage_allows(row, kind)

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
    saved = await _record_file(row, kind, file_id, path, original_name, mime, size, user_id)
    if kind == "SCRIPT":
        # A new script file enables the script stage on the post and resets it to DRAFT so it goes through review.
        pool = get_pool()
        async with pool.acquire() as conn:
            await conn.execute(
                "UPDATE content_submissions SET script_required = TRUE, script_status = 'DRAFT' WHERE id = $1",
                submission_id,
            )
            if row.get("script_status") not in (None, "DRAFT"):
                await _add_event(conn, submission_id, "SCRIPT_REPLACED",
                                 f"New script uploaded ({original_name}); it needs review again", user_id)
    return saved


def _require_script_stage_allows(row: Dict[str, Any], kind: str) -> None:
    """On a post with a script stage, the video waits for the approved script."""
    if not script_stage_on(row):
        return
    if kind == "MAIN" and row.get("script_status") != "APPROVED":
        raise HTTPException(status_code=409, detail="Get the script approved first, then upload the video.")


async def _record_file(
    row: Dict[str, Any],
    kind: str,
    file_id: UUID,
    path: Path,
    original_name: str,
    mime: str,
    size: int,
    user_id: UUID,
    canva: Optional[Dict[str, Any]] = None,
    replaces_file_id: Optional[UUID] = None,
) -> SubmissionFile:
    """Save a stored file to the submission (an upload or a Canva export): replace the main file or script it
    supersedes, log it, and start transcription for a main video. canva: the design id, title and revision."""
    submission_id = row["id"]
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
            if replaces_file_id:
                # A reimport replaces exactly the file it came from (one image of a carousel, say).
                await conn.execute(
                    "UPDATE content_submission_files SET deleted_at = NOW() WHERE id = $1 AND submission_id = $2",
                    replaces_file_id, submission_id,
                )
            # Single-asset types keep one main file: replacing it soft-deletes the previous one.
            elif kind == "MAIN" and row["content_type"] != "CAROUSEL":
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
                     transcript_status, transcript_error, transcript_updated_at,
                     source, canva_design_id, canva_design_title, canva_revision)
                VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, CASE WHEN $9::varchar IS NULL THEN NULL ELSE NOW() END,
                        $11, $12, $13, $14)
                RETURNING id, kind, original_name, mime_type, size_bytes, created_at, transcript_status, transcript_error,
                          source, canva_design_id, canva_design_title, canva_revision
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
                "CANVA" if canva else "UPLOAD",
                canva["design_id"] if canva else None,
                canva["title"] if canva else None,
                canva["revision"] if canva else None,
            )
            await conn.execute(
                "UPDATE content_submissions SET updated_at = NOW(), content_edited_at = NOW(), updated_by = $2 WHERE id = $1",
                submission_id, user_id)
            if canva:
                verb = "Reimported" if replaces_file_id else "Imported"
                await _add_event(conn, submission_id, "CANVA_IMPORTED",
                                 f"{verb} \"{canva['title']}\" from Canva (revision {canva['revision']})", user_id,
                                 {"file_id": str(file_id), **canva, "reimport": bool(replaces_file_id)})
            else:
                await _add_event(conn, submission_id, "FILE_ADDED", f"Added {original_name}", user_id,
                                 {"file_id": str(file_id), "kind": kind, "size_bytes": size})
    if needs_transcript and not transcript_blocked:
        transcription_service.schedule([file_id])
    if image_text_service.is_image_file(kind, mime):
        image_text_service.schedule([file_id])
    if document_text_service.is_document_file(kind, original_name):
        document_text_service.schedule([file_id])
    return _file_out(submission_id, dict(saved))


# ---------------------------------------------------------------------------
# Canva import
# ---------------------------------------------------------------------------

async def _store_canva_export(row: Dict[str, Any], export: canva_service.DesignExport, user_id: UUID,
                              replaces_file_id: Optional[UUID] = None) -> SubmissionFile:
    """Write a Canva export into the submission's storage and record it as the main file."""
    _require_script_stage_allows(row, "MAIN")
    allowed = MAIN_EXT_BY_TYPE.get(row["content_type"], set())
    if export.extension not in allowed:
        kind = "a video" if export.design.kind == "VIDEO" else "an image"
        raise HTTPException(status_code=415,
                            detail=f"That design exports as {kind}, which doesn't fit a {row['content_type'].replace('_', ' ').lower()} submission")
    if len(export.data) > MAX_BYTES["MAIN"]:
        raise HTTPException(status_code=413, detail="The Canva export is too large")
    file_id = uuid4()
    folder = STORAGE_ROOT / str(row["id"])
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"{file_id}{export.extension}"
    async with aiofiles.open(path, "wb") as out:
        await out.write(export.data)
    canva = {"design_id": export.design.id, "title": export.design.title, "revision": export.design.revision}
    try:
        return await _record_file(row, "MAIN", file_id, path, export.file_name, export.mime_type, len(export.data),
                                  user_id, canva=canva, replaces_file_id=replaces_file_id)
    except BaseException:
        path.unlink(missing_ok=True)
        raise


async def import_from_canva(submission_id: UUID, user_id: UUID, design_id: str) -> Submission:
    """Export a Canva design and add it as the submission's main file, like an upload."""
    row = await _get_owned_row(submission_id, user_id)
    _require_editable(row)
    export = await canva_service.export_design(user_id, design_id)
    await _store_canva_export(row, export, user_id)
    if (row["title"] or "").strip() in ("", "Untitled submission"):
        await fetch_one("UPDATE content_submissions SET title = $2 WHERE id = $1 RETURNING id",
                        submission_id, export.design.title[:300])
    return await get_submission(submission_id, user_id)


async def reimport_from_canva(submission_id: UUID, user_id: UUID, resubmit: bool = False) -> Submission:
    """Bring in the latest revision of the Canva design the main file came from, replacing it. With resubmit (for
    content sent back for changes) it then goes straight back to review as the next version: the rulebook check
    runs again and the reviewers are notified, with earlier versions, comments and decisions kept in the history."""
    row = await _get_owned_row(submission_id, user_id)
    _require_editable(row)
    linked = await fetch_one(
        """
        SELECT id, canva_design_id, canva_revision FROM content_submission_files
        WHERE submission_id = $1 AND kind = 'MAIN' AND source = 'CANVA' AND deleted_at IS NULL
        ORDER BY created_at DESC LIMIT 1
        """,
        submission_id,
    )
    if not linked:
        raise HTTPException(status_code=409, detail="This submission's content wasn't imported from Canva")
    export = await canva_service.export_design(user_id, linked["canva_design_id"])
    if export.design.revision == linked["canva_revision"]:
        raise HTTPException(status_code=409,
                            detail=f"No changes in Canva since the last import (revision {export.design.revision})")
    await _store_canva_export(row, export, user_id, replaces_file_id=linked["id"])
    if resubmit and row["status"] == "CHANGES_REQUESTED":
        submission, _job = await submit_for_review(submission_id, user_id)
        return submission
    return await get_submission(submission_id, user_id)


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
    if script_stage_on(row) and row.get("script_status") == "IN_REVIEW":
        kind = await fetch_one("SELECT kind FROM content_submission_files WHERE id = $1", file_id)
        if kind and kind["kind"] == "SCRIPT":
            raise HTTPException(status_code=409, detail="The script is with a reviewer. Wait for their reply to change it.")
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
            await conn.execute("UPDATE content_submissions SET content_edited_at = NOW() WHERE id = $1", submission_id)
            await _add_event(conn, submission_id, "FILE_REMOVED", f"Removed {name}", user_id, {"file_id": str(file_id)})


async def get_file_for_download(submission_id: UUID, file_id: UUID, user_id: UUID) -> Tuple[Path, str, str]:
    await _get_owned_row(submission_id, user_id, manager_can_read=True)
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

def analysis_pending(check: Optional[Dict[str, Any]]) -> bool:
    """Whether the post is still being analysed: its video transcript or the text in its images isn't read yet.
    Until it is, the check has no score to show; it's re-checked automatically when the reading finishes."""
    if not check:
        return False
    if "analysis_pending" in check:
        return bool(check["analysis_pending"])
    return (bool(check.get("transcript_pending")) or (check.get("images_pending") or 0) > 0
            or (check.get("documents_pending") or 0) > 0)


def ai_unavailable(check: Optional[Dict[str, Any]]) -> bool:
    """Whether the AI couldn't read part of the post, so the check has no score to show."""
    return bool(check and check.get("ai_unavailable"))


def _unavailable_sql(column: str) -> str:
    return f"coalesce(({column}->>'ai_unavailable')::boolean, false)"


def _pending_sql(column: str) -> str:
    """SQL for analysis_pending() on a stored check (older checks only have the two parts)."""
    return (
        f"coalesce(({column}->>'analysis_pending')::boolean, "
        f"coalesce(({column}->>'transcript_pending')::boolean, false) OR coalesce(({column}->>'images_pending')::int, 0) > 0"
        f" OR coalesce(({column}->>'documents_pending')::int, 0) > 0)"
    )


def _check_text(row: Dict[str, Any]) -> str:
    return "\n\n".join(p for p in [row["title"], row["caption_text"]] if p and p.strip())


def check_request(row: Dict[str, Any], text: str, has_video: bool, has_images: bool,
                  sources: Sequence[ProvidedSource] = ()) -> ComplianceCheckRequest:
    """The rulebook check request for this submission's scope (rule sets, brand, audience, region, platforms), with the
    documents attached as evidence for the source check."""
    return ComplianceCheckRequest(
        text=text,
        sources=list(sources),
        video_title=row["title"],
        content_type=f"{row['content_type']}_{(row['platforms'] or ['GENERAL'])[0]}",
        rule_sets=list(row["rule_sets"] or []),
        brand_codes=brands_of(row),
        audiences=audiences_of(row),
        region=row["region"],
        platforms=list(row["platforms"] or []),
        # For rules limited by "applies to": its type, what's actually attached, and whether it's Shaun's own post.
        content_parts=compliance_parts_for(
            row["content_type"],
            has_video=has_video,
            has_images=has_images,
            is_ceo=CEO_BRAND in brands_of(row)
            or approval_path_service.is_ceo_content(row.get("rule_sets") or [], _accounts(row)),
        ),
    )


def script_stage_on(row: Dict[str, Any]) -> bool:
    """Video posts created since the script stage existed have their script approved before the video."""
    return bool(row.get("script_required")) and row.get("content_type") == "VIDEO"


async def approved_script(row: Dict[str, Any]) -> Optional[str]:
    """The approved script's text, when the post went through the script stage and it was approved."""
    if row.get("script_status") != "APPROVED":
        return None
    found = await fetch_one(
        "SELECT body FROM content_script_versions WHERE submission_id = $1 ORDER BY version_number DESC LIMIT 1",
        row["id"],
    )
    return found["body"] if found else None


async def _run_check(row: Dict[str, Any]) -> Tuple[Any, Dict[str, Any]]:
    """The one rulebook check every screen shows: title, caption, the script (or a blog's article file), the video
    transcript and the text shown in the post's images (each once it's been read), against the rules scoped to this
    submission. Includes per-segment
    flags for the reviewer's timeline, and says what was read and which rules need a person."""
    video = await fetch_one(
        """
        SELECT transcript, transcript_status FROM content_submission_files
        WHERE submission_id = $1 AND kind = 'MAIN' AND deleted_at IS NULL AND mime_type LIKE 'video/%'
        ORDER BY created_at DESC LIMIT 1
        """,
        row["id"],
    )
    segments: List[Dict[str, Any]] = []
    if video and video["transcript_status"] == "COMPLETED":
        raw = video["transcript"]
        segments = (json.loads(raw) if isinstance(raw, str) else raw) or []
    spoken = " ".join(str(s.get("text", "")) for s in segments).strip()
    # The post's images, in the order it shows them; any never read are read now and re-checked when done.
    images = await fetch_all(
        """
        SELECT id, image_text, image_text_status FROM content_submission_files
        WHERE submission_id = $1 AND kind = ANY($2::text[]) AND deleted_at IS NULL AND mime_type LIKE 'image/%'
        ORDER BY (kind <> 'MAIN'), created_at
        """,
        row["id"], list(image_text_service.IMAGE_KINDS),
    )
    await image_text_service.schedule_missing([i["id"] for i in images if i["image_text_status"] in (None, "PROCESSING")])
    image_texts = [
        f"Text in image {n}: {i['image_text']}"
        for n, i in enumerate(images, start=1)
        if i["image_text_status"] == "COMPLETED" and (i["image_text"] or "").strip()
    ]
    # The script, and a blog's article file: read in the background like image text, re-checked when done.
    documents = await fetch_all(
        """
        SELECT id, kind, original_name, doc_text, doc_text_status, doc_text_error FROM content_submission_files
        WHERE submission_id = $1 AND kind IN ('SCRIPT', 'MAIN') AND deleted_at IS NULL
          AND lower(original_name) ~ '\\.(txt|md|docx|pdf|doc)$'
        ORDER BY (kind <> 'SCRIPT'), created_at
        """,
        row["id"],
    )
    await document_text_service.schedule_missing([d["id"] for d in documents if d["doc_text_status"] in (None, "PROCESSING")])
    # Once the script is approved, the approved wording (which may include the reviewer's edits) is what's checked.
    script = await approved_script(row)
    # Once the video's speech is read, the video is scored on what it says: the script was already checked and
    # approved on its own, and how far the video follows it is reported separately (script_match).
    video_scored = bool(script and spoken)
    document_texts = [
        f"{'Script' if d['kind'] == 'SCRIPT' else 'Article'}:\n{d['doc_text']}"
        for d in documents
        if d["doc_text_status"] == "COMPLETED" and (d["doc_text"] or "").strip()
        and not (script and d["kind"] == "SCRIPT")
    ]
    if script and not video_scored:
        document_texts.insert(0, f"Script:\n{script}")
    # Scripts and articles carry their page, heading and table marks; the check reads the wording only.
    text = document_layout.plain_text(
        "\n\n".join(p for p in [_check_text(row), *document_texts, spoken, *image_texts] if p)
    ) or row["title"]
    # Documents attached as evidence (Supporting documents): what the post's claims are checked against, with the links it
    # cites. Read in the background like the script, and re-checked when done.
    evidence = await fetch_all(
        """
        SELECT id, original_name, doc_text, doc_text_status FROM content_submission_files
        WHERE submission_id = $1 AND kind = 'SUPPORTING' AND deleted_at IS NULL
          AND lower(original_name) ~ '\\.(txt|md|docx|pdf)$'
        ORDER BY created_at
        """,
        row["id"],
    )
    await document_text_service.schedule_missing([d["id"] for d in evidence if d["doc_text_status"] in (None, "PROCESSING")])
    sources = [
        ProvidedSource(name=d["original_name"], text=d["doc_text"])
        for d in evidence if d["doc_text_status"] == "COMPLETED" and (d["doc_text"] or "").strip()
    ]
    result = await check_compliance(
        check_request(row, text, has_video=bool(video), has_images=bool(images), sources=sources)
    )
    # Does the video say what the approved script says? Only once the script is approved and the transcript read.
    match = script_match.compare(script, spoken) if script and segments else None
    script_match.flag_mismatch(result, match)
    check = {
        **result.model_dump(mode="json"),
        "checked_at": datetime.now(timezone.utc).isoformat(),
        "includes_transcript": bool(segments),
        # A video whose transcript isn't ready yet is re-checked automatically when it is.
        "transcript_pending": bool(video) and video["transcript_status"] not in ("COMPLETED", "FAILED"),
        # What was read from the images: how many there are, how many were read, and how many are still being
        # read (re-checked automatically when done) or couldn't be read (their text wasn't checked).
        "images_total": len(images),
        "images_read": sum(1 for i in images if i["image_text_status"] == "COMPLETED"),
        "images_pending": sum(1 for i in images if i["image_text_status"] in (None, "PROCESSING")),
        "images_failed": sum(1 for i in images if i["image_text_status"] == "FAILED"),
        # The same for the script (and a blog's article file). Failed ones say why, so the creator can fix it.
        "includes_script": not video_scored and (
            bool(script) or any(d["kind"] == "SCRIPT" and d["doc_text_status"] == "COMPLETED" for d in documents)
        ),
        "documents_total": len(documents),
        "documents_read": sum(1 for d in documents if d["doc_text_status"] == "COMPLETED"),
        "documents_pending": sum(1 for d in documents if d["doc_text_status"] in (None, "PROCESSING")),
        "documents_failed": [
            {"name": d["original_name"], "error": d["doc_text_error"]}
            for d in documents if d["doc_text_status"] == "FAILED"
        ],
        "segment_flags": flag_segments(segments, result.violations),
        # A context rule matched but no AI model could read it (key out of credits, quota used up, service down):
        # the score would be a guess, so screens show a message instead of it (re-run the check once the AI is back).
        "ai_unavailable": result.ai_unavailable,
        "script_match": match,
    }
    # No score until the whole post has been read; screens show "Under review" until then.
    check["analysis_pending"] = analysis_pending(check)
    return result, check


async def _mirror_to_script_version(conn, submission_id: UUID, check: Dict[str, Any]) -> None:
    """A check saved on the post while its script is with the reviewer is also the script version's check, so both
    screens show the same score (a check run twice at once can't leave them apart)."""
    await conn.execute(
        """
        UPDATE content_script_versions v SET check_result = $2::jsonb
        FROM content_submissions s
        WHERE s.id = $1 AND v.submission_id = s.id
          AND s.script_status IN ('IN_REVIEW', 'CHANGES_REQUESTED', 'EDITS_PROPOSED')
          AND v.version_number = (SELECT max(version_number) FROM content_script_versions WHERE submission_id = $1)
        """,
        submission_id, json.dumps(check),
    )


async def run_initial_check(submission_id: UUID, user_id: UUID) -> Submission:
    row = await _get_owned_row(submission_id, user_id)
    if not _check_text(row):
        raise HTTPException(status_code=422, detail="Add a title or caption before running the check")
    await require_details_complete(row, "running the compliance check")
    # Which models answered is kept with the result, so it can be shown later and not only while the check runs.
    attempts: Dict[str, Dict[str, Any]] = {}
    outer = ai_observability.attempt_listener.get()
    started = time.perf_counter()

    def record(event: Dict[str, Any]) -> None:
        if event.get("event") == "attempt" and event.get("id"):
            attempts[event["id"]] = {k: event.get(k) for k in ("id", "status", "model", "feature", "attempt", "tier", "error_kind", "error", "duration_ms")}
        if outer is not None:
            outer(event)

    token = ai_observability.attempt_listener.set(record)
    try:
        result, check = await _run_check(row)
    finally:
        ai_observability.attempt_listener.reset(token)
    check["models"] = {"attempts": list(attempts.values()), "duration_ms": round((time.perf_counter() - started) * 1000)}
    pool = get_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            await conn.execute("UPDATE content_submissions SET latest_check = $2::jsonb, updated_at = NOW() WHERE id = $1",
                               submission_id, json.dumps(check))
            await _mirror_to_script_version(conn, submission_id, check)
            await _add_event(conn, submission_id, "CHECK_RUN",
                             "Initial check: the AI review couldn't run, so there is no score yet" if check["ai_unavailable"]
                             else f"Initial check: {result.violations_count} issue(s), score {result.score}/100", user_id,
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
    if not _account_ids(row):
        missing.append("account_id")
    if not row["rule_sets"]:
        missing.append("rule_sets")
    if not row.get("approval_needed_by"):
        missing.append("approval_needed_by")
    if not row.get("reviewer_ids"):
        missing.append("reviewer_ids")
    has_main = await fetch_one(
        "SELECT 1 AS ok FROM content_submission_files WHERE submission_id = $1 AND kind = 'MAIN' AND deleted_at IS NULL LIMIT 1",
        row["id"],
    )
    has_text = bool((row["caption_text"] or "").strip())
    if row["content_type"] in MAIN_FILE_REQUIRED and not has_main:
        missing.append("main_file")
    # A social post can't carry a video (one attached before this was a rule has to come off, or the type changes).
    if row["content_type"] == "SOCIAL_POST" and await fetch_one(
        "SELECT 1 AS ok FROM content_submission_files WHERE submission_id = $1 AND kind = 'MAIN' AND deleted_at IS NULL "
        "AND mime_type LIKE 'video/%' LIMIT 1",
        row["id"],
    ):
        missing.append("main_file")
    # Every post needs its caption (the copy, for text posts): it's what the score and the sentiment analysis read
    # first, so the check never runs on an image alone.
    if not has_text:
        missing.append("caption")
    if script_stage_on(row) and row.get("script_status") != "APPROVED":
        missing.append("script_approval")
    return missing


# What sending needs on top of the post's own details; the checks don't wait for these.
_SEND_ONLY_FIELDS = {"approval_needed_by", "reviewer_ids"}


async def details_missing(row: Dict[str, Any]) -> List[str]:
    """What the post still needs before its compliance check, sentiment analysis and public mood can be run."""
    return [m for m in await _missing_for_submit(row) if m not in _SEND_ONLY_FIELDS]


async def require_details_complete(row: Dict[str, Any], doing: str) -> None:
    """The three tools read a finished post, so they are refused until its details are filled in. A post that has been
    sent was complete when it went, so it is never refused."""
    if row["status"] not in post_steps_service.EDITABLE_STATUSES:
        return
    missing = await details_missing(row)
    if missing:
        raise HTTPException(
            status_code=422,
            detail=f"Fill in the post details before {doing}. Still needed: {post_steps_service.missing_words(missing)}.",
        )


async def require_details_complete_for(submission_id: UUID, user_id: UUID, doing: str) -> None:
    await require_details_complete(await _get_owned_row(submission_id, user_id, manager_can_read=True), doing)


async def _require_not_duplicate(row: Dict[str, Any], confirm_repost: bool) -> None:
    """The same content and caption, already sent for approval to the same place on the same date, can't be sent
    again; on another date it can, once the creator confirms (see duplicate_service)."""
    report = await duplicate_service.find(row)
    if report.blocking:
        m = report.blocking
        raise HTTPException(status_code=409, detail={
            "code": "DUPLICATE_POST",
            "message": f"The same post and caption were already sent for approval as {m.reference_code} with the same "
                       "submission details and publish date. Change the brand, account, audience, region or platform "
                       "so they no longer overlap.",
            "duplicates": report.model_dump(mode="json"),
        })
    if report.repost and not confirm_repost:
        m = report.repost
        raise HTTPException(status_code=409, detail={
            "code": "REPOST_CONFIRM",
            "message": f"The same post was already sent for approval as {m.reference_code} for another publish date. "
                       "Confirm to send it for this date too.",
            "duplicates": report.model_dump(mode="json"),
        })


def _review_job(r: Dict[str, Any]) -> ReviewJob:
    return ReviewJob(
        id=r["id"],
        version_number=r["version_number"],
        status=r["status"],
        reviewer_route=list(r["reviewer_route"] or []),
        traffic_light=r["traffic_light"],
        # No score while the post is still being read (see analysis_pending).
        score=None if analysis_pending(_json(r["automated_check"])) or ai_unavailable(_json(r["automated_check"])) else r["score"],
        automated_check=_json(r["automated_check"]),
        created_at=r["created_at"],
        completed_at=r["completed_at"],
        decision=r.get("decision"),
        decision_notes=r.get("decision_notes"),
        decided_by_name=r.get("decided_by_name"),
        decided_at=r.get("decided_at"),
        manual_checks=_json(r.get("manual_checks")) or [],
    )


async def submit_for_review(submission_id: UUID, user_id: UUID,
                            confirm_repost: bool = False) -> Tuple[Submission, ReviewJob]:
    row = await _get_owned_row(submission_id, user_id)
    if row["status"] == "IN_REVIEW":
        raise HTTPException(status_code=409, detail="This submission is already in review")
    _require_editable(row)

    missing = await _missing_for_submit(row)
    account_ids = _account_ids(row)
    accounts = await _connected_accounts(account_ids)
    if len(accounts) != len(account_ids):
        missing.append("account_id")
    account = accounts[0] if accounts else None
    stray = [a for a in accounts if row["platforms"] and not _account_covers(a, list(row["platforms"]))]
    if stray:
        raise HTTPException(status_code=422, detail={
            "message": "Some publishing accounts post to platforms that aren't selected: "
                       + ", ".join(a.label for a in stray) + ".",
            "missing": ["platforms"],
        })
    now = datetime.now(timezone.utc)
    if row["publish_at"] and row["publish_at"] < now:
        missing.append("publish_at")
    needed_by = row.get("approval_needed_by")
    if needed_by and needed_by < now:
        missing.append("approval_needed_by")
    if needed_by and row["publish_at"] and needed_by > row["publish_at"] and "publish_at" not in missing:
        raise HTTPException(status_code=422, detail={
            "message": "The approval date has to be on or before the publish date.",
            "missing": ["approval_needed_by"],
        })
    if missing:
        raise HTTPException(status_code=422, detail={
            "message": "Some required information is missing.",
            "missing": sorted(set(missing)),
        })
    await _require_not_duplicate(row, confirm_repost)

    # Sent to the reviewers picked (required, see _missing_for_submit) who can still review it, and its final
    # approvers, who must see it to approve it.
    reviewer_ids = list(row.get("reviewer_ids") or [])
    if reviewer_ids:
        creator_out = await _creator_exclusion(row["created_by"])
        eligible = set(await _reviewer_ids(exclude=creator_out, brand_codes=brands_of(row)))
        reviewer_ids = [r for r in reviewer_ids if r in eligible]
        if not reviewer_ids:
            raise HTTPException(status_code=422, detail={
                "message": "None of the reviewers you picked can review this brand any more. Pick reviewers again.",
                "missing": ["reviewer_ids"],
            })
        approver_ids, _ = _approvers(await approval_path_service.load_rules(), row, _accounts(row))
        reviewer_ids += [a for a in approver_ids if a not in reviewer_ids and a != creator_out]
    row = {**dict(row), "reviewer_ids": reviewer_ids}

    # Automated rulebook screening is the first step of the review job.
    result, check = await _run_check(row)

    files = await _list_files(submission_id)
    # The script's text as read now, kept with the version so later versions can be compared with it.
    script = await fetch_one(
        """
        SELECT doc_text FROM content_submission_files
        WHERE submission_id = $1 AND kind = 'SCRIPT' AND deleted_at IS NULL AND doc_text_status = 'COMPLETED'
        ORDER BY created_at DESC LIMIT 1
        """,
        submission_id,
    )
    script_text = await approved_script(row) or (script["doc_text"] if script else None)
    version = row["current_version"] + 1
    snapshot = {
        "title": row["title"],
        "content_type": row["content_type"],
        "brand_code": row["brand_code"],
        "brand_codes": brands_of(row),
        "audience": row["audience"],
        "audiences": audiences_of(row),
        "region": row["region"],
        "platforms": list(row["platforms"] or []),
        "account": account.model_dump(mode="json") if account else None,
        "accounts": [a.model_dump(mode="json") for a in accounts],
        "publish_at": row["publish_at"].isoformat() if row["publish_at"] else None,
        "approval_needed_by": needed_by.isoformat() if needed_by else None,
        "caption_html": row["caption_html"],
        "caption_text": row["caption_text"],
        "script_text": script_text,
        "rule_sets": list(row["rule_sets"] or []),
        "reviewer_route": list(row["reviewer_route"] or []),
        "reviewer_ids": [str(r) for r in reviewer_ids],
        "files": [
            {"id": str(f["id"]), "kind": f["kind"], "original_name": f["original_name"],
             "mime_type": f["mime_type"], "size_bytes": f["size_bytes"],
             **({"source": "CANVA", "canva_design_id": f["canva_design_id"], "canva_revision": f["canva_revision"]}
                if f["source"] == "CANVA" else {})}
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
                submission_id, version, list(row["reviewer_route"] or []), json.dumps(check),
                result.traffic_light, result.score, user_id,
            )
            await conn.execute(
                """
                UPDATE content_submissions
                SET status = 'IN_REVIEW', current_version = $2, latest_check = $3::jsonb,
                    submitted_at = NOW(), updated_at = NOW(), updated_by = $4,
                    due_at = $5, due_kind = 'REVIEW', due_reminded_at = NULL, due_overdue_notified_at = NULL,
                    reviewer_ids = $6::uuid[]
                WHERE id = $1
                """,
                submission_id, version, json.dumps(check), user_id,
                review_deadline_service.due_date("REVIEW", row["publish_at"], requested=needed_by),
                reviewer_ids,
            )
            await _add_event(conn, submission_id, "SUBMITTED", f"Version {version} submitted for review", user_id,
                             {"version": version, "review_job_id": str(job["id"])})
            await _add_event(conn, submission_id, "CHECK_COMPLETED",
                             _check_summary(result, check), user_id,
                             {"traffic_light": result.traffic_light, "score": result.score})
            await _add_event(conn, submission_id, "REVIEW_REQUESTED", "Sent to the review queue", user_id)

    await log_audit_event("content_submission", str(submission_id), "SUBMIT_FOR_REVIEW", user_id,
                          {"reference_code": row["reference_code"], "version": version,
                           "traffic_light": result.traffic_light, "score": result.score})
    try:
        from services.notifications.notification_service import create_notification
        await create_notification(NotificationCreate(
            user_id=str(user_id),
            type="SUBMISSION",
            title=f"{row['reference_code']} submitted for review",
            message=f"\"{row['title']}\" (version {version}) is in the review queue. {_score_line(result, check)}",
            link_url=f"/submissions/{submission_id}",
            entity_type="content_submission",
            entity_id=str(submission_id),
        ))
    except Exception:
        logger.warning("Could not create submission notification", exc_info=True)
    await _notify_reviewers(row, version, _score_line(result, check), user_id)
    # Sentiment analysis isn't started here: the reviewer runs it from the review screen when they want it.

    return await get_submission(submission_id, user_id), _review_job(dict(job))


async def withdraw(submission_id: UUID, user_id: UUID) -> Submission:
    """Pull a submission back to draft so the creator can edit and resubmit: from review, or after approval for a
    late fix. Withdrawing approved content gives up the approval; the edited version goes through review again."""
    row = await _get_owned_row(submission_id, user_id)
    if row["status"] not in ("IN_REVIEW", "APPROVED"):
        raise HTTPException(status_code=409, detail="Only content that's in review or approved can be withdrawn")
    was_approved = row["status"] == "APPROVED"
    version = row["current_version"]
    pool = get_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            current = await conn.fetchval("SELECT status FROM content_submissions WHERE id = $1 FOR UPDATE", submission_id)
            if current != row["status"]:
                raise HTTPException(status_code=409, detail="Its status just changed; refresh to see it")
            # An approved version's review job keeps its APPROVED decision as history; the event records the withdrawal.
            await conn.execute(
                """
                UPDATE content_review_jobs SET status = 'WITHDRAWN', completed_at = NOW()
                WHERE submission_id = $1 AND status = 'AWAITING_REVIEW'
                """,
                submission_id,
            )
            await conn.execute(
                """
                UPDATE content_submissions SET status = 'DRAFT', updated_at = NOW(), updated_by = $2,
                    due_at = NULL, due_kind = NULL
                WHERE id = $1
                """,
                submission_id, user_id,
            )
            await _add_event(
                conn, submission_id, "WITHDRAWN",
                f"Approved version {version} withdrawn for a late fix; the approval no longer applies"
                if was_approved else f"Version {version} withdrawn from review for editing",
                user_id, {"version": version, "was_approved": was_approved},
            )
    await log_audit_event("content_submission", str(submission_id),
                          "WITHDRAW_APPROVED_SUBMISSION" if was_approved else "WITHDRAW_SUBMISSION", user_id,
                          {"reference_code": row["reference_code"], "version": version})
    if was_approved:
        # Tell whoever approved it that it's coming back for another look.
        approver = await fetch_one(
            """
            SELECT decided_by FROM content_review_jobs
            WHERE submission_id = $1 AND version_number = $2 AND decision = 'APPROVE' AND decided_by IS NOT NULL
            ORDER BY decided_at DESC LIMIT 1
            """,
            submission_id, version,
        )
        if approver and approver["decided_by"] != user_id:
            await _notify(
                [approver["decided_by"]],
                f"{row['reference_code']} was withdrawn after approval",
                f"\"{row['title']}\" was pulled back for a late fix. It's off the calendar until it's resubmitted "
                "and approved again.",
                f"/submissions/{submission_id}",
                submission_id,
            )
    return await get_submission(submission_id, user_id)


# The Content Screener's tabs: content waiting on a reviewer, content waiting on its creator, and decided content.
QUEUE_TAB_STATUSES = {
    "review": ("IN_REVIEW",),
    "sent_back": ("CHANGES_REQUESTED",),
    "done": ("APPROVED", "REJECTED"),
}
# Decided content stays in the Screener's Done tab this long; the Content Portal keeps everything.
DONE_DAYS = 30

# When the latest version was decided (approved or sent back), for the Done tab's window and order.
_DECIDED_AT = """(SELECT max(j.decided_at) FROM content_review_jobs j
                  WHERE j.submission_id = s.id AND j.version_number = s.current_version)"""

_QUEUE_ORDER = {
    # Overdue first, then the soonest due; anything without a deadline after, oldest submission first.
    "review": "s.due_at ASC NULLS LAST, s.submitted_at ASC",
    "sent_back": "s.due_at ASC NULLS LAST, s.updated_at DESC",
    "done": "decided_at DESC NULLS LAST, s.updated_at DESC",
}


def _queue_filter(tab: str) -> str:
    statuses = ", ".join(f"'{st}'" for st in QUEUE_TAB_STATUSES[tab])
    cond = f"s.status IN ({statuses})"
    if tab == "done":
        cond += f" AND COALESCE({_DECIDED_AT}, s.updated_at) >= NOW() - INTERVAL '{DONE_DAYS} days'"
    return cond


# What the dashboard's activity feed shows for each event (other events, like re-checks, are routine).
_ACTIVITY_ACTIONS = """
    CASE e.event_type
        WHEN 'SUBMITTED' THEN CASE WHEN (e.data->>'version')::int > 1 THEN 'resubmitted' ELSE 'submitted' END
        WHEN 'REVIEW_DECISION' THEN CASE e.data->>'decision' WHEN 'APPROVE' THEN 'approved' ELSE 'requested changes on' END
        WHEN 'APPROVAL_REOPENED' THEN 'reopened the approval of'
        WHEN 'RESCHEDULED' THEN 'rescheduled'
        WHEN 'FLAG_DISMISSED' THEN 'dismissed a compliance flag on'
        WHEN 'WITHDRAWN' THEN 'withdrew'
    END
"""
DASHBOARD_ACTIVITY_LIMIT = 8
DASHBOARD_DAYS = 30


async def dashboard_summary(user_id: UUID, mine: bool = False) -> DashboardSummary:
    """Recent activity and last-30-days compliance insights, over the content the viewer can see: everyone's on
    their brands for reviewers, Content Managers and super admins, otherwise their own (as in the Content Portal).
    mine=True limits it to the viewer's own content (a super admin viewing the app as a creator)."""
    see_all = not mine and await _can_see_team_content(user_id)
    scope = await _brand_scope(user_id)
    visible = (
        "s.deleted_at IS NULL AND (s.created_by = $1 OR ($2 AND ($3::text[] IS NULL OR s.brand_codes && $3::text[])"
        f" AND {_sent_to()}))"
    )
    since = f"NOW() - INTERVAL '{DASHBOARD_DAYS} days'"
    args = (user_id, see_all, scope)

    activity, decisions, scores, findings, script_decisions = await asyncio.gather(
        fetch_all(
            f"""
            SELECT * FROM (
                SELECT e.created_at AS at, e.actor_user_id AS actor_id, {_ACTIVITY_ACTIONS} AS action,
                       s.id AS submission_id, s.reference_code, s.title
                FROM content_submission_events e JOIN content_submissions s ON s.id = e.submission_id
                WHERE {visible}
                UNION ALL
                SELECT c.created_at, c.author_id, 'commented on', s.id, s.reference_code, s.title
                FROM content_review_comments c JOIN content_submissions s ON s.id = c.submission_id
                WHERE {visible} AND c.deleted_at IS NULL
            ) a
            LEFT JOIN LATERAL (SELECT full_name FROM users WHERE id = a.actor_id) u ON true
            WHERE a.action IS NOT NULL
            ORDER BY a.at DESC
            LIMIT {DASHBOARD_ACTIVITY_LIMIT}
            """,
            *args,
        ),
        fetch_one(
            f"""
            SELECT count(*) AS completed,
                   count(*) FILTER (WHERE j.decision = 'APPROVE') AS approved,
                   count(*) FILTER (WHERE j.decision = 'REQUEST_CHANGES') AS changes,
                   (SELECT count(*) FROM content_submissions s
                    WHERE {visible} AND s.status = 'IN_REVIEW' AND s.due_kind = 'REVIEW' AND s.due_at < NOW()) AS overdue
            FROM content_review_jobs j JOIN content_submissions s ON s.id = j.submission_id
            WHERE {visible} AND j.decision IS NOT NULL AND j.decided_at >= {since}
            """,
            *args,
        ),
        fetch_one(
            f"""
            SELECT round(avg(j.score))::int AS average
            FROM content_review_jobs j JOIN content_submissions s ON s.id = j.submission_id
            WHERE {visible} AND j.score IS NOT NULL AND NOT {_pending_sql("j.automated_check")} AND NOT {_unavailable_sql("j.automated_check")} AND j.created_at >= {since}
            """,
            *args,
        ),
        fetch_all(
            f"""
            SELECT v->>'rule_number' AS rule_number, max(v->>'title') AS title, count(*) AS count
            FROM content_review_jobs j JOIN content_submissions s ON s.id = j.submission_id,
                 jsonb_array_elements(COALESCE(j.automated_check->'violations', '[]'::jsonb)) v
            WHERE {visible} AND j.created_at >= {since} AND v->>'rule_number' IS NOT NULL
              AND coalesce(v->>'status', 'VIOLATION') = 'VIOLATION'
            GROUP BY 1 ORDER BY 3 DESC, 1 LIMIT 5
            """,
            *args,
        ),
        # A reviewer's call on a script counts as a review too: approved, or sent back (with edits or a note).
        fetch_one(
            f"""
            SELECT count(*) AS completed,
                   count(*) FILTER (WHERE v.review_decision = 'APPROVED') AS approved,
                   count(*) FILTER (WHERE v.review_decision IN ('EDITED', 'CHANGES_REQUESTED')) AS changes
            FROM content_script_versions v JOIN content_submissions s ON s.id = v.submission_id
            WHERE {visible} AND v.review_decision IS NOT NULL AND v.reviewed_at >= {since}
            """,
            *args,
        ),
    )
    return DashboardSummary(
        activity=[
            DashboardActivity(
                at=r["at"], actor_id=r["actor_id"], actor_name=r["full_name"], action=r["action"],
                submission_id=r["submission_id"], reference_code=r["reference_code"], title=r["title"],
            )
            for r in activity
        ],
        reviews_completed=decisions["completed"] + script_decisions["completed"],
        approved=decisions["approved"] + script_decisions["approved"],
        changes_requested=decisions["changes"] + script_decisions["changes"],
        overdue_reviews=decisions["overdue"],
        average_score=scores["average"],
        common_findings=[DashboardFinding(rule_number=r["rule_number"], title=r["title"] or "", count=r["count"]) for r in findings],
        days=DASHBOARD_DAYS,
    )


async def review_queue_counts(user_id: UUID, mine: bool = False) -> ReviewQueueCounts:
    """How many posts are in each Screener tab, for the tab labels (mine=True: only the user's own)."""
    see_all = not mine and await _can_see_team_content(user_id)
    row = await fetch_one(
        f"""
        SELECT
            count(*) FILTER (WHERE {_queue_filter("review")}) AS review,
            count(*) FILTER (WHERE {_queue_filter("sent_back")}) AS sent_back,
            count(*) FILTER (WHERE {_queue_filter("done")}) AS done,
            count(*) FILTER (WHERE s.script_status = 'IN_REVIEW' AND s.script_required AND s.content_type = 'VIDEO')
                AS scripts,
            count(*) FILTER (WHERE s.script_status IN ('CHANGES_REQUESTED', 'EDITS_PROPOSED') AND s.script_required
                AND s.content_type = 'VIDEO') AS scripts_sent_back,
            count(*) FILTER (WHERE s.script_status = 'APPROVED' AND s.status = 'DRAFT' AND s.script_required
                AND s.content_type = 'VIDEO' AND s.updated_at > NOW() - INTERVAL '30 days') AS scripts_done
        FROM content_submissions s
        WHERE s.deleted_at IS NULL
          AND (s.created_by = $1 OR ($2 AND ($3::text[] IS NULL OR s.brand_codes && $3::text[]) AND {_sent_to()}))
        """,
        user_id,
        see_all,
        await _brand_scope(user_id),
    )
    return ReviewQueueCounts(**dict(row))


async def review_queue(user_id: UUID, tab: str = "review", search: Optional[str] = None,
                       mine: bool = False) -> List[ReviewQueueItem]:
    """Submitted posts of every type for one Content Screener tab: everyone's for reviewers, Content Managers and
    super admins, otherwise the user's own. Done covers the last 30 days and can be searched by title or reference.
    Each comes with its video (if any) and its images in order. mine=True shows only the user's own content, as its
    creator (no review actions), for a super admin viewing the app as a creator."""
    if tab not in QUEUE_TAB_STATUSES:
        raise HTTPException(status_code=422, detail="Unknown queue tab")
    see_all = not mine and await _can_see_team_content(user_id)
    is_reviewer = not mine and await _can_review(user_id)
    is_super = await _is_super_admin(user_id)
    term = (search or "").strip()
    rows = await fetch_all(
        f"""
        SELECT s.id, s.reference_code, s.title, s.content_type, s.status, s.platforms, s.caption_text, s.latest_check,
               s.current_version, s.submitted_at, s.created_by, s.publish_at, s.due_at, s.due_kind,
               s.brand_code, s.brand_codes, s.audience, s.audiences, s.rule_sets, s.approval_needed_by, s.script_required, s.script_status,
               s.reviewer_ids,
               {_DECIDED_AT} AS decided_at,
               u.full_name AS creator_name,
               c.id AS acc_id, c.provider AS acc_provider, c.account_name AS acc_account_name,
               c.connection_metadata AS acc_connection_metadata,
               f.id AS file_id, f.kind AS file_kind, f.original_name AS file_original_name,
               f.mime_type AS file_mime_type, f.size_bytes AS file_size_bytes, f.created_at AS file_created_at,
               f.transcript AS file_transcript, f.transcript_status AS file_transcript_status,
               f.transcript_error AS file_transcript_error
        FROM content_submissions s
        LEFT JOIN LATERAL (
            SELECT * FROM content_submission_files cf
            WHERE cf.submission_id = s.id AND cf.kind = 'MAIN' AND cf.deleted_at IS NULL
              AND cf.mime_type LIKE 'video/%'
            ORDER BY cf.created_at DESC
            LIMIT 1
        ) f ON true
        LEFT JOIN oauth_connections c ON c.id = s.account_id
        LEFT JOIN users u ON u.id = s.created_by
        WHERE {_queue_filter(tab)} AND s.deleted_at IS NULL
          AND (s.created_by = $1 OR ($2 AND ($3::text[] IS NULL OR s.brand_codes && $3::text[]) AND {_sent_to()}))
          AND ($4 = '' OR s.title ILIKE '%' || $4 || '%' OR s.reference_code ILIKE '%' || $4 || '%')
        ORDER BY {_QUEUE_ORDER[tab]}
        LIMIT 100
        """,
        user_id,
        see_all,
        await _brand_scope(user_id),
        term,
    )
    rules = await approval_path_service.load_rules()
    standing = await flag_review_service.approval_standing([dict(r) for r in rows])
    # Videos uploaded before transcription existed (or whose attempt was abandoned) get transcribed now.
    await transcription_service.schedule_missing(
        [r["file_id"] for r in rows if r["file_id"] and r["file_transcript_status"] not in ("COMPLETED", "FAILED")]
    )
    # Every listed post's images in one query, in the order the post shows them.
    image_rows = await fetch_all(
        """
        SELECT * FROM content_submission_files
        WHERE submission_id = ANY($1::uuid[]) AND kind IN ('MAIN', 'IMAGE') AND deleted_at IS NULL
          AND mime_type LIKE 'image/%'
        ORDER BY submission_id, (kind <> 'MAIN'), created_at
        """,
        [r["id"] for r in rows],
    ) if rows else []
    images: Dict[UUID, List[SubmissionFile]] = {}
    for img in image_rows:
        images.setdefault(img["submission_id"], []).append(_file_out(img["submission_id"], dict(img)))
    # Images uploaded before their text was read (or whose read was abandoned) are read now; the check of
    # each open item updates when that's done.
    await image_text_service.schedule_missing([img["id"] for img in image_rows if img["image_text_status"] in (None, "PROCESSING")])
    # Posts still being read: what's left and when their score should be ready.
    progress = await analysis_progress_service.for_submissions(
        [r["id"] for r in rows if analysis_pending(_json(r["latest_check"]))]
    )
    edges = await delegation_service.edges_for([r["id"] for r in rows if r["status"] == "IN_REVIEW"])
    return [
        ReviewQueueItem(
            id=r["id"],
            reference_code=r["reference_code"],
            title=r["title"],
            content_type=r["content_type"],
            status=r["status"],
            platforms=list(r["platforms"] or []),
            account=_account_option(r, prefix="acc_"),
            caption_text=r["caption_text"],
            latest_check=_json(r["latest_check"]),
            analysis_progress=progress.get(r["id"]),
            current_version=r["current_version"],
            submitted_at=r["submitted_at"],
            creator_name=r["creator_name"],
            video=_file_out(r["id"], {
                "id": r["file_id"], "kind": r["file_kind"], "original_name": r["file_original_name"],
                "mime_type": r["file_mime_type"], "size_bytes": r["file_size_bytes"], "created_at": r["file_created_at"],
                "transcript": r["file_transcript"], "transcript_error": r["file_transcript_error"],
                # schedule_missing may have just queued it; report that rather than "never transcribed".
                "transcript_status": r["file_transcript_status"] or "PENDING",
            }) if r["file_id"] else None,
            images=images.get(r["id"], []),
            can_review=r["status"] == "IN_REVIEW" and is_reviewer and (r["created_by"] != user_id or is_super),
            **_approval_fields(rules, r, user_id, is_reviewer, is_super, edges=edges.get(r["id"])),
            reviewer_ids=list(r["reviewer_ids"] or []),
            open_critical_flags=standing[r["id"]].open_critical if r["id"] in standing else 0,
            approval_score=standing[r["id"]].score if r["id"] in standing else None,
            min_approval_score=flag_review_service.MIN_APPROVAL_SCORE,
            publish_at=r["publish_at"],
            brand_code=r["brand_code"],
            brand_codes=brands_of(r),
            audience=r["audience"],
            audiences=audiences_of(r),
            approval_needed_by=r["approval_needed_by"],
            **_due(r),
            created_by=r["created_by"],
            can_manage_approval=r["status"] == "APPROVED" and is_reviewer and (r["created_by"] != user_id or is_super),
            decided_at=r["decided_at"],
            script_status=(r["script_status"] or "DRAFT") if script_stage_on(r) else None,
        )
        for r in rows
    ]


async def get_version(submission_id: UUID, version: int, user_id: UUID) -> VersionView:
    """A past (or the current) submitted version, for looking back at what it was and how it was decided. Files
    replaced later are still served, so the old video plays."""
    row = await _get_owned_row(submission_id, user_id, manager_can_read=True)
    v = await fetch_one(
        """
        SELECT v.version_number, v.snapshot, v.created_at, u.full_name AS created_by_name
        FROM content_submission_versions v LEFT JOIN users u ON u.id = v.created_by
        WHERE v.submission_id = $1 AND v.version_number = $2
        """,
        submission_id, version,
    )
    if not v:
        raise HTTPException(status_code=404, detail=f"Version {version} doesn't exist")
    snapshot = _json(v["snapshot"]) or {}
    main = next((f for f in snapshot.get("files") or [] if f.get("kind") == "MAIN"), None)
    video = None
    if main:
        f = await fetch_one(
            """
            SELECT id, kind, original_name, mime_type, size_bytes, created_at,
                   transcript, transcript_status, transcript_error
            FROM content_submission_files WHERE id = $1 AND submission_id = $2
            """,
            UUID(main["id"]), submission_id,
        )
        if f:
            video = _file_out(submission_id, dict(f))
    job = await fetch_one(
        """
        SELECT j.*, u.full_name AS decided_by_name
        FROM content_review_jobs j LEFT JOIN users u ON u.id = j.decided_by
        WHERE j.submission_id = $1 AND j.version_number = $2
        ORDER BY j.created_at DESC LIMIT 1
        """,
        submission_id, version,
    )
    return VersionView(
        version_number=v["version_number"],
        latest_version=row["current_version"],
        snapshot=snapshot,
        created_at=v["created_at"],
        created_by_name=v["created_by_name"],
        video=video,
        check=_json(job["automated_check"]) if job else None,
        review_job=_review_job(dict(job)) if job else None,
    )


async def get_history(submission_id: UUID, user_id: UUID) -> SubmissionHistory:
    await _get_owned_row(submission_id, user_id, manager_can_read=True)
    versions = await fetch_all(
        """
        SELECT v.version_number, v.snapshot, v.created_at, u.full_name AS created_by_name
        FROM content_submission_versions v LEFT JOIN users u ON u.id = v.created_by
        WHERE v.submission_id = $1 ORDER BY v.version_number DESC
        """,
        submission_id,
    )
    jobs = await fetch_all(
        """
        SELECT j.*, u.full_name AS decided_by_name
        FROM content_review_jobs j LEFT JOIN users u ON u.id = j.decided_by
        WHERE j.submission_id = $1 ORDER BY j.created_at DESC
        """,
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


# ---------------------------------------------------------------------------
# Review: reviewer notifications, decisions, comments, re-checks
# ---------------------------------------------------------------------------

REVIEW_GROUP = "CONTENT_REVIEW_PUBLISH"


async def _reviewer_ids(exclude: Optional[UUID] = None, brand_code: Optional[str] = None,
                        brand_codes: Optional[List[str]] = None) -> List[UUID]:
    """Active users who can review: super admins and roles with "Review & Publish Content". With brands, only
    reviewers who work on at least one of them (or on every brand)."""
    brands = list(brand_codes or ([brand_code] if brand_code else [])) or None
    rows = await fetch_all(
        """
        SELECT u.id FROM users u
        WHERE u.is_active AND u.deleted_at IS NULL AND ($1::uuid IS NULL OR u.id <> $1)
          AND (
            $3::text[] IS NULL OR u.is_super_admin
            OR NOT EXISTS (SELECT 1 FROM user_brands ub WHERE ub.user_id = u.id)
            OR EXISTS (SELECT 1 FROM user_brands ub WHERE ub.user_id = u.id AND ub.brand_code = ANY($3::text[]))
          )
          AND (
            u.is_super_admin
            OR EXISTS (
                SELECT 1 FROM user_roles ur JOIN roles r ON r.id = ur.role_id
                WHERE ur.user_id = u.id AND ur.is_active AND r.is_active AND r.deleted_at IS NULL
                  AND (r.code = 'SUPER_ADMIN' OR EXISTS (
                        SELECT 1 FROM role_permission_groups rpg
                        JOIN permission_groups g ON g.id = rpg.permission_group_id
                        WHERE rpg.role_id = r.id AND g.code = $2))
            )
          )
        """,
        exclude, REVIEW_GROUP, brands,
    )
    return [r["id"] for r in rows]


async def _creator_exclusion(creator_id: UUID) -> Optional[UUID]:
    """Who is left out of the reviewers a post can go to: its creator, except a super admin, who can already review
    their own posts (see the review checks) and so can pick themselves, e.g. to test the review flow."""
    return None if await _is_super_admin(creator_id) else creator_id


async def reviewer_options(user_id: UUID, brand_codes: Optional[List[str]] = None) -> List[ReviewerOption]:
    """The people a creator can send content to: reviewers and super admins (on any of the brands, when given). Not
    themselves, unless they're a super admin."""
    ids = await _reviewer_ids(exclude=await _creator_exclusion(user_id), brand_codes=brand_codes)
    rows = await fetch_all(
        "SELECT id, full_name, email, is_super_admin FROM users WHERE id = ANY($1::uuid[]) ORDER BY full_name, email",
        ids,
    )
    return [
        ReviewerOption(id=r["id"], name=r["full_name"] or r["email"], email=r["email"], is_super_admin=bool(r["is_super_admin"]))
        for r in rows
    ]


async def _review_recipients(row: Dict[str, Any], exclude: Optional[UUID]) -> List[UUID]:
    """Who gets a post's review notifications: the reviewers it was sent to (still able to review), or every
    reviewer on its brand when it wasn't sent to anyone in particular."""
    everyone = await _reviewer_ids(exclude=exclude, brand_codes=brands_of(row))
    picked = list(row.get("reviewer_ids") or [])
    return [r for r in picked if r in set(everyone)] if picked else everyone


async def _notify(user_ids: List[UUID], title: str, message: str, link: str, submission_id: UUID) -> None:
    if not user_ids:
        return
    try:
        import asyncio
        from services.notifications.notification_service import create_notification
        await asyncio.gather(*(
            create_notification(NotificationCreate(
                user_id=str(uid), type="SUBMISSION", title=title, message=message, link_url=link,
                entity_type="content_submission", entity_id=str(submission_id),
            ))
            for uid in user_ids
        ))
    except Exception:
        logger.warning("Could not send review notifications for %s", submission_id, exc_info=True)


def _score_line(result: Any, check: Dict[str, Any]) -> str:
    """The check's score for a message, or that it's still being analysed."""
    if analysis_pending(check):
        return "Automated check: under review (still reading the video or images)."
    if ai_unavailable(check):
        return "Automated check: no score, the AI review couldn't run."
    return f"Automated check: {result.score}/100."


def _check_summary(result: Any, check: Dict[str, Any]) -> str:
    if analysis_pending(check):
        return "Automated rulebook check started; the score comes once the video and images have been read"
    if ai_unavailable(check):
        return "Automated rulebook check ran without the AI review, so there is no score yet"
    return f"Automated rulebook check: {result.violations_count} issue(s), score {result.score}/100"


async def _notify_reviewers(row: Dict[str, Any], version: int, score_line: str, submitter: UUID) -> None:
    author = await fetch_one("SELECT full_name FROM users WHERE id = $1", submitter)
    again = " (resubmitted)" if version > 1 else ""
    who = author["full_name"] if author else "Someone"
    reviewers = await _review_recipients(row, exclude=submitter)
    approver_ids, _ = _approvers(await approval_path_service.load_rules(), row, _accounts(row))
    approvers = [a for a in approver_ids if a != submitter]
    await _notify(
        approvers,
        f"{row['reference_code']} needs your approval{again}",
        f"{who} sent \"{row['title']}\" (version {version}) for review, and you give final approval for it. "
        f"{score_line}",
        f"/submissions/{row['id']}",
        row["id"],
    )
    await _notify(
        [r for r in reviewers if r not in approvers],
        f"{row['reference_code']} is ready for review{again}",
        f"{who} sent \"{row['title']}\" (version {version}) for review. {score_line}",
        f"/submissions/{row['id']}",
        row["id"],
    )


async def decide_review(submission_id: UUID, user_id: UUID, req: ReviewDecisionRequest) -> Submission:
    """A reviewer approves the version in review (scheduling it on the calendar, or to publish now) or sends it
    back to the creator with what to change."""
    row = await _get_owned_row(submission_id, user_id, manager_can_read=True)
    if not await _can_review(user_id):
        raise HTTPException(status_code=403, detail="Only reviewers can approve content or request changes")
    if row["status"] != "IN_REVIEW":
        raise HTTPException(status_code=409, detail="Only content that's in review can be approved or sent back")
    is_super = await _is_super_admin(user_id)
    if row["created_by"] == user_id and not is_super:
        raise HTTPException(status_code=403, detail="You can't review your own content; another reviewer needs to")
    approval_score: Optional[int] = None
    if req.decision == "APPROVE":
        # Nothing can be approved on a check that wasn't read in full: with the AI down every finding is only a
        # "needs review", so there'd be no score to hold to the minimum.
        saved = _json(row["latest_check"]) or {}
        if analysis_pending(saved):
            raise HTTPException(
                status_code=422,
                detail="The post is still being read, so it has no compliance score yet. Approve it once the check has finished.",
            )
        if ai_unavailable(saved):
            raise HTTPException(
                status_code=422,
                detail="The AI review couldn't run, so this has no compliance score and can't be approved yet. It's "
                       "re-checked automatically once the AI is back. You can still request changes.",
            )
        standing = (await flag_review_service.approval_standing([row])).get(row["id"])
        open_critical = standing.open_critical if standing else 0
        if open_critical:
            raise HTTPException(
                status_code=422,
                detail=f"{open_critical} critical compliance flag{'s are' if open_critical > 1 else ' is'} still open. "
                       "Request changes to fix it, or dismiss the flag with a reason if it's a false alarm.",
            )
        # Flags dismissed as false alarms give their points back; what's left must reach the minimum.
        if standing and standing.score_too_low:
            raise HTTPException(
                status_code=422,
                detail=f"The rulebook score is {standing.score}/100; approval needs at least {standing.min_score}. "
                       "Request changes to fix the flagged wording, or dismiss flags with a reason if they're false alarms.",
            )
        approval_score = standing.score if standing else None
        approver_ids, approver_names = _approvers(await approval_path_service.load_rules(), row, _accounts(row))
        # A named final approver who delegated the post passes their approval to the delegate.
        approver_ids = await delegation_service.with_delegates(submission_id, approver_ids)
        if not _may_approve(user_id, is_super, approver_ids):
            raise HTTPException(
                status_code=403,
                detail=f"Final approval for this content is with {', '.join(approver_names)}. "
                       "You can still comment or request changes.",
            )

    version = row["current_version"]
    notes = (req.notes or "").strip() or None
    now = datetime.now(timezone.utc)
    publish_at = row["publish_at"]
    manual_checks: Optional[List[Dict[str, str]]] = None
    if req.decision == "REQUEST_CHANGES":
        open_comments = await fetch_one(
            """
            SELECT count(*) AS n FROM content_review_comments
            WHERE submission_id = $1 AND version_number = $2 AND deleted_at IS NULL AND resolved_at IS NULL
            """,
            submission_id, version,
        )
        if not notes and not open_comments["n"]:
            raise HTTPException(status_code=422, detail="Add a note or comments saying what needs to change")
        new_status, label = "CHANGES_REQUESTED", "Changes requested"
    else:
        if req.publish_now:
            publish_at, label = now, "Approved to publish now"
        else:
            publish_at = req.publish_at or row["publish_at"]
            if not publish_at:
                raise HTTPException(status_code=422, detail="Pick a date and time to schedule it")
            if publish_at < now:
                raise HTTPException(status_code=422, detail="Pick a publish time in the future, or publish it now")
            label = f"Approved and scheduled for {publish_at.astimezone(timezone.utc):%b %d, %H:%M} UTC"
        new_status = "APPROVED"
        # Rules the automated check can't test (image editing, image rights, ...): the approving reviewer
        # confirms each one, and that's recorded with the decision.
        needed = (_json(row["latest_check"]) or {}).get("manual_review_rules") or []
        confirmed = {r.strip() for r in req.confirmed_rules}
        unconfirmed = [m for m in needed if m.get("rule_number") not in confirmed]
        if unconfirmed:
            raise HTTPException(
                status_code=422,
                detail="Confirm you've checked the rules the automated check can't test before approving: "
                       + ", ".join(f"{m['rule_number']} ({m['title']})" for m in unconfirmed),
            )
        manual_checks = [{"rule_number": m["rule_number"], "title": m["title"]} for m in needed]

    pool = get_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            current = await conn.fetchval(
                "SELECT status FROM content_submissions WHERE id = $1 FOR UPDATE", submission_id
            )
            if current != "IN_REVIEW":
                raise HTTPException(status_code=409, detail="Someone else just decided on this; refresh to see it")
            await conn.execute(
                """
                UPDATE content_review_jobs
                SET status = $3, decision = $4, decision_notes = $5, decided_by = $6, decided_at = NOW(),
                    completed_at = NOW(), manual_checks = $7::jsonb
                WHERE submission_id = $1 AND version_number = $2 AND status = 'AWAITING_REVIEW'
                """,
                submission_id, version, new_status, req.decision, notes, user_id,
                json.dumps(manual_checks) if manual_checks is not None else None,
            )
            revision_due = (
                review_deadline_service.revision_due(publish_at, req.revision_days, req.revision_due_at)
                if new_status == "CHANGES_REQUESTED" else None
            )
            await conn.execute(
                """
                UPDATE content_submissions
                SET status = $2, publish_at = $3, updated_at = NOW(), updated_by = $4,
                    due_at = $5, due_kind = CASE WHEN $5::timestamptz IS NULL THEN NULL ELSE 'REVISION' END,
                    due_reminded_at = NULL, due_overdue_notified_at = NULL
                WHERE id = $1
                """,
                submission_id, new_status, publish_at, user_id, revision_due,
            )
            await _add_event(conn, submission_id, "REVIEW_DECISION", f"Version {version}: {label}", user_id,
                             {"decision": req.decision, "notes": notes, "version": version,
                              "revision_days": req.revision_days,
                              "revision_due": revision_due.isoformat() if revision_due else None,
                              "publish_at": publish_at.isoformat() if publish_at else None,
                              "publish_now": req.publish_now, "manual_checks": manual_checks,
                              "approval_score": approval_score})

    await log_audit_event("content_submission", str(submission_id), f"REVIEW_{req.decision}", user_id,
                          {"reference_code": row["reference_code"], "version": version, "notes": notes,
                           "publish_at": publish_at.isoformat() if publish_at else None,
                           "manual_checks": manual_checks, "approval_score": approval_score})

    reviewer = await fetch_one("SELECT full_name FROM users WHERE id = $1", user_id)
    who = reviewer["full_name"] if reviewer else "A reviewer"
    if req.decision == "REQUEST_CHANGES":
        title = f"{row['reference_code']} needs changes"
        message = (f"{who} asked for changes to \"{row['title']}\"." + (f" \"{notes}\"" if notes else " See the comments.")
                   + f" Please resubmit by {review_deadline_service._when(revision_due)}.")
        link = f"/submissions/{submission_id}"
    else:
        title = f"{row['reference_code']} was approved"
        message = f"{who} approved \"{row['title']}\". {label}."
        link = "/content-calendar"
    if row["created_by"] != user_id:
        await _notify([row["created_by"]], title, message, link, submission_id)
    # The decision becomes precedent for analysing similar content.
    review_memory_service.remember_soon(submission_id, version)
    return await get_submission(submission_id, user_id)


async def _approved_row_for_reviewer(submission_id: UUID, user_id: UUID) -> Dict[str, Any]:
    """An approved submission the user may manage as a reviewer (not their own upload, unless super admin)."""
    row = await _get_owned_row(submission_id, user_id, manager_can_read=True)
    if not await _can_review(user_id):
        raise HTTPException(status_code=403, detail="Only reviewers can change approved content")
    if row["status"] != "APPROVED":
        raise HTTPException(status_code=409, detail="Only approved content can be rescheduled or reopened")
    if row["created_by"] == user_id and not await _is_super_admin(user_id):
        raise HTTPException(status_code=403, detail="You can't manage the approval of your own content")
    return row


async def reopen_approved(submission_id: UUID, user_id: UUID, req: ReopenRequest) -> Submission:
    """A reviewer takes back an approval (for example a late legal concern) and sends the content to its creator
    as changes requested. The approved version's decision becomes the request for changes."""
    row = await _approved_row_for_reviewer(submission_id, user_id)
    version = row["current_version"]
    notes = req.notes.strip()
    if not notes:
        raise HTTPException(status_code=422, detail="Say what needs to change")
    revision_due = review_deadline_service.revision_due(row["publish_at"], req.revision_days, req.revision_due_at)
    pool = get_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            current = await conn.fetchval("SELECT status FROM content_submissions WHERE id = $1 FOR UPDATE", submission_id)
            if current != "APPROVED":
                raise HTTPException(status_code=409, detail="Its status just changed; refresh to see it")
            previous = await conn.fetchrow(
                """
                SELECT decision_notes, decided_by, decided_at FROM content_review_jobs
                WHERE submission_id = $1 AND version_number = $2
                ORDER BY created_at DESC LIMIT 1
                """,
                submission_id, version,
            )
            await conn.execute(
                """
                UPDATE content_review_jobs
                SET status = 'CHANGES_REQUESTED', decision = 'REQUEST_CHANGES', decision_notes = $3,
                    decided_by = $4, decided_at = NOW(), completed_at = NOW()
                WHERE id = (SELECT id FROM content_review_jobs WHERE submission_id = $1 AND version_number = $2
                            ORDER BY created_at DESC LIMIT 1)
                """,
                submission_id, version, notes, user_id,
            )
            await conn.execute(
                """
                UPDATE content_submissions SET status = 'CHANGES_REQUESTED', updated_at = NOW(), updated_by = $2,
                    due_at = $3, due_kind = 'REVISION', due_reminded_at = NULL, due_overdue_notified_at = NULL
                WHERE id = $1
                """,
                submission_id, user_id, revision_due,
            )
            await _add_event(conn, submission_id, "APPROVAL_REOPENED",
                             f"Approval of version {version} reopened: changes requested", user_id,
                             {"version": version, "notes": notes, "revision_days": req.revision_days,
                              "revision_due": revision_due.isoformat(),
                              # The approval it replaces, so the history still shows who approved it and when.
                              "previous_approval": {
                                  "notes": previous["decision_notes"] if previous else None,
                                  "decided_by": str(previous["decided_by"]) if previous and previous["decided_by"] else None,
                                  "decided_at": previous["decided_at"].isoformat() if previous and previous["decided_at"] else None,
                              }})
    await log_audit_event("content_submission", str(submission_id), "REOPEN_APPROVAL", user_id,
                          {"reference_code": row["reference_code"], "version": version, "notes": notes})
    reviewer = await fetch_one("SELECT full_name FROM users WHERE id = $1", user_id)
    await _notify(
        [row["created_by"]],
        f"{row['reference_code']} needs changes after all",
        f"{reviewer['full_name'] if reviewer else 'A reviewer'} reopened the approval of \"{row['title']}\": \"{notes}\""
        f" Please resubmit by {review_deadline_service._when(revision_due)}.",
        f"/submissions/{submission_id}",
        submission_id,
    )
    review_memory_service.remember_soon(submission_id, version)
    return await get_submission(submission_id, user_id)


async def reschedule_approved(submission_id: UUID, user_id: UUID, req: RescheduleRequest) -> Submission:
    """A reviewer moves approved content to a new publish time. The approval stands; only the date changes."""
    row = await _approved_row_for_reviewer(submission_id, user_id)
    publish_at = req.publish_at if req.publish_at.tzinfo else req.publish_at.replace(tzinfo=timezone.utc)
    if publish_at < datetime.now(timezone.utc):
        raise HTTPException(status_code=422, detail="Pick a publish time in the future")
    before = row["publish_at"]
    pool = get_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            updated = await conn.fetchval(
                """
                UPDATE content_submissions SET publish_at = $2, updated_at = NOW(), updated_by = $3
                WHERE id = $1 AND status = 'APPROVED' RETURNING id
                """,
                submission_id, publish_at, user_id,
            )
            if not updated:
                raise HTTPException(status_code=409, detail="Its status just changed; refresh to see it")
            label = f"{publish_at.astimezone(timezone.utc):%b %d, %H:%M} UTC"
            await _add_event(conn, submission_id, "RESCHEDULED", f"Rescheduled to {label}", user_id,
                             {"from": before.isoformat() if before else None, "to": publish_at.isoformat()})
    await log_audit_event("content_submission", str(submission_id), "RESCHEDULE", user_id,
                          {"reference_code": row["reference_code"],
                           "from": before.isoformat() if before else None, "to": publish_at.isoformat()})
    if row["created_by"] != user_id:
        await _notify(
            [row["created_by"]],
            f"{row['reference_code']} was rescheduled",
            f"\"{row['title']}\" now publishes {label}.",
            "/content-calendar",
            submission_id,
        )
    return await get_submission(submission_id, user_id)


def _comment_out(r: Dict[str, Any], viewer: UUID, viewer_is_super: bool) -> ReviewComment:
    return ReviewComment(
        id=r["id"],
        submission_id=r["submission_id"],
        version_number=r["version_number"],
        category=r["category"],
        body=r["body"],
        start_sec=float(r["start_sec"]) if r["start_sec"] is not None else None,
        end_sec=float(r["end_sec"]) if r["end_sec"] is not None else None,
        author_id=r["author_id"],
        author_name=r.get("author_name"),
        # Stored when written; older comments fall back to "anyone but the creator".
        from_reviewer=r["from_reviewer"] if r.get("from_reviewer") is not None else r["author_id"] != r["submission_created_by"],
        resolved_at=r["resolved_at"],
        resolved_by_name=r.get("resolved_by_name"),
        created_at=r["created_at"],
        can_delete=r["author_id"] == viewer or viewer_is_super,
    )


_COMMENT_SELECT = """
    SELECT c.*, s.created_by AS submission_created_by, a.full_name AS author_name, rb.full_name AS resolved_by_name
    FROM content_review_comments c
    JOIN content_submissions s ON s.id = c.submission_id
    LEFT JOIN users a ON a.id = c.author_id
    LEFT JOIN users rb ON rb.id = c.resolved_by
"""


async def list_comments(submission_id: UUID, user_id: UUID) -> List[ReviewComment]:
    await _get_owned_row(submission_id, user_id, manager_can_read=True)
    rows = await fetch_all(
        _COMMENT_SELECT + " WHERE c.submission_id = $1 AND c.deleted_at IS NULL ORDER BY c.created_at",
        submission_id,
    )
    is_super = await _is_super_admin(user_id)
    return [_comment_out(r, user_id, is_super) for r in rows]


async def _comment_access(submission_id: UUID, user_id: UUID) -> Dict[str, Any]:
    """Reviewers and the creator can comment and resolve comments."""
    row = await _get_owned_row(submission_id, user_id, manager_can_read=True)
    if row["created_by"] != user_id and not await _can_review(user_id):
        raise HTTPException(status_code=403, detail="Only reviewers and the person who uploaded it can comment")
    return row


async def add_comment(submission_id: UUID, user_id: UUID, data: ReviewCommentCreate) -> ReviewComment:
    row = await _comment_access(submission_id, user_id)
    if data.start_sec is not None and data.end_sec is not None and data.end_sec < data.start_sec:
        raise HTTPException(status_code=422, detail="The end time is before the start time")
    # Someone else's comment is a reviewer's. The creator's own counts as a reviewer's only when they review it
    # themselves (a super admin, while it's in review); otherwise it's a reply.
    from_reviewer = row["created_by"] != user_id or (row["status"] == "IN_REVIEW" and await _can_review(user_id))
    saved = await fetch_one(
        """
        INSERT INTO content_review_comments
            (submission_id, version_number, author_id, category, body, start_sec, end_sec, from_reviewer)
        VALUES ($1, $2, $3, $4, $5, $6, $7, $8)
        RETURNING id
        """,
        # A draft's notes belong to the version it becomes, so the first one is version 1, not 0.
        submission_id, max(row["current_version"], 1), user_id, data.category, data.body.strip(),
        data.start_sec, data.end_sec, from_reviewer,
    )
    full = await fetch_one(_COMMENT_SELECT + " WHERE c.id = $1", saved["id"])
    return _comment_out(full, user_id, await _is_super_admin(user_id))


async def set_comment_resolved(submission_id: UUID, comment_id: UUID, user_id: UUID, resolved: bool) -> ReviewComment:
    await _comment_access(submission_id, user_id)
    updated = await fetch_one(
        """
        UPDATE content_review_comments
        SET resolved_at = CASE WHEN $3 THEN NOW() ELSE NULL END,
            resolved_by = CASE WHEN $3 THEN $4::uuid ELSE NULL END, updated_at = NOW()
        WHERE id = $1 AND submission_id = $2 AND deleted_at IS NULL
        RETURNING id
        """,
        comment_id, submission_id, resolved, user_id,
    )
    if not updated:
        raise HTTPException(status_code=404, detail="Comment not found")
    full = await fetch_one(_COMMENT_SELECT + " WHERE c.id = $1", comment_id)
    return _comment_out(full, user_id, await _is_super_admin(user_id))


async def delete_comment(submission_id: UUID, comment_id: UUID, user_id: UUID) -> None:
    await _get_owned_row(submission_id, user_id, manager_can_read=True)
    is_super = await _is_super_admin(user_id)
    deleted = await fetch_one(
        """
        UPDATE content_review_comments SET deleted_at = NOW(), updated_at = NOW()
        WHERE id = $1 AND submission_id = $2 AND deleted_at IS NULL AND (author_id = $3 OR $4)
        RETURNING id
        """,
        comment_id, submission_id, user_id, is_super,
    )
    if not deleted:
        raise HTTPException(status_code=404, detail="Comment not found, or it isn't yours to delete")


# ---------------------------------------------------------------------------
# Re-checking open content when the rulebook changes
# ---------------------------------------------------------------------------

_rulebook_recheck: Optional["asyncio.Task[None]"] = None
RULEBOOK_RECHECK_DELAY_SEC = 3


async def _recheck_one(row: Dict[str, Any], describe) -> Tuple[Any, Dict[str, Any], bool]:
    """Re-run one item's check and save it wherever it's shown (the post, its script version, its review job).
    describe(before, result, check) returns (log line, details) to log on the item, or None to log nothing."""
    before = _json(row["latest_check"]) or {}
    result, check = await _run_check(row)
    logged = describe(before, result, check)
    pool = get_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            await conn.execute(
                "UPDATE content_submissions SET latest_check = $2::jsonb WHERE id = $1", row["id"], json.dumps(check)
            )
            await _mirror_to_script_version(conn, row["id"], check)
            await conn.execute(
                """
                UPDATE content_review_jobs SET automated_check = $3::jsonb, score = $4, traffic_light = $5
                WHERE submission_id = $1 AND version_number = $2 AND status = 'AWAITING_REVIEW'
                """,
                row["id"], row["current_version"], json.dumps(check), result.score, result.traffic_light,
            )
            if logged:
                await _add_event(conn, row["id"], "CHECK_COMPLETED", logged[0], None, logged[1])
    return result, check, bool(logged)


async def recheck_open_submissions(reason: str) -> int:
    """Re-run the check on content waiting for a reviewer or its creator, so saved scores follow the rulebook.
    Approved and draft content keep their check (drafts are checked again when they're sent). Only real changes
    are logged on the item."""
    rows = await fetch_all(
        "SELECT * FROM content_submissions WHERE status IN ('IN_REVIEW', 'CHANGES_REQUESTED') AND deleted_at IS NULL"
    )

    def describe(before: Dict[str, Any], result: Any, check: Dict[str, Any]):
        moved = before.get("score") != result.score or sorted(
            v.get("rule_number", "") for v in before.get("violations") or []
        ) != sorted(v.rule_number for v in result.violations)
        if not moved:
            return None
        return (
            f"Re-checked after a rulebook change ({reason}): score {before.get('score', '?')} -> {result.score}"
            + (" (blocked: critical rule)" if result.blocked else ""),
            {"traffic_light": result.traffic_light, "score": result.score, "reason": "rulebook_change"},
        )

    changed = 0
    for row in rows:
        _, _, moved = await _recheck_one(dict(row), describe)
        changed += moved
    if rows:
        logger.info("Re-checked %d open submission(s) after a rulebook change; %d changed", len(rows), changed)
    return changed


# How often to look for checks the AI couldn't read, and how many to retry in one pass.
AI_RECHECK_SEC = max(60.0, float(os.getenv("AI_RECHECK_MINUTES", "10")) * 60)
AI_RECHECK_BATCH = int(os.getenv("AI_RECHECK_BATCH", "5"))


async def recheck_ai_unavailable() -> int:
    """Re-run the checks the AI couldn't read (its key out of credits, quota used up, the service down), oldest first.
    Stops at the first one that still can't be read, since the AI is then still down. Returns how many were read."""
    rows = await fetch_all(
        f"""
        SELECT * FROM content_submissions s
        WHERE s.deleted_at IS NULL AND {_unavailable_sql("s.latest_check")}
          AND (s.status IN ('IN_REVIEW', 'CHANGES_REQUESTED')
               OR (s.status = 'DRAFT' AND s.script_status IN ('IN_REVIEW', 'CHANGES_REQUESTED', 'EDITS_PROPOSED')))
        ORDER BY s.updated_at
        LIMIT {AI_RECHECK_BATCH}
        """
    )

    def describe(before: Dict[str, Any], result: Any, check: Dict[str, Any]):
        if check.get("ai_unavailable"):
            return None  # still unread: nothing to report yet
        return (
            f"Re-checked now that the AI review is available: score {result.score}/100"
            + (" (blocked: critical rule)" if result.blocked else ""),
            {"traffic_light": result.traffic_light, "score": result.score, "reason": "ai_available"},
        )

    read = 0
    for row in rows:
        _, check, _ = await _recheck_one(dict(row), describe)
        if ai_unavailable(check):
            break
        read += 1
    return read


async def ai_recheck_loop() -> None:
    """Keep retrying checks the AI couldn't read until it can, so nothing waits on a person to re-run it."""
    import asyncio

    logger.info("Re-checking AI-unavailable checks every %.0f s", AI_RECHECK_SEC)
    await asyncio.sleep(60)
    while True:
        try:
            read = await recheck_ai_unavailable()
            if read:
                logger.info("Re-checked %d submission(s) the AI couldn't read earlier", read)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Couldn't re-check the checks the AI couldn't read")
        await asyncio.sleep(AI_RECHECK_SEC)


async def recheck_outdated_on_start() -> None:
    """On start, re-check open content whose saved check predates the current rulebook or built-in claim checks
    (e.g. after a deploy that changed the checks), so its score follows them."""
    current = await compliance_rulebook_version()
    stale = await fetch_one(
        """
        SELECT count(*) AS n FROM content_submissions
        WHERE status IN ('IN_REVIEW', 'CHANGES_REQUESTED') AND deleted_at IS NULL
          AND latest_check->>'rulebook_version' IS DISTINCT FROM $1
        """,
        current,
    )
    if stale and stale["n"]:
        await recheck_open_submissions("the automated checks were updated")


def schedule_rulebook_recheck(reason: str) -> None:
    """Re-check open content shortly after a rule changes; a burst of edits leads to one pass."""
    import asyncio

    global _rulebook_recheck

    async def run() -> None:
        await asyncio.sleep(RULEBOOK_RECHECK_DELAY_SEC)
        try:
            await recheck_open_submissions(reason)
        except Exception:
            logger.exception("Couldn't re-check open submissions after a rulebook change")

    if _rulebook_recheck and not _rulebook_recheck.done():
        _rulebook_recheck.cancel()
    _rulebook_recheck = asyncio.create_task(run())


async def recheck_after_transcript(file_id: UUID) -> None:
    """When a video's transcript is ready, re-run the check so the score includes what's said and shown.
    Approved and withdrawn items keep the check they were decided on."""
    await _recheck_for_file(file_id, ["MAIN"], "the video transcript")


async def recheck_after_image_text(file_id: UUID) -> None:
    """When an image's text has been read (or couldn't be), re-run the check so the score includes it."""
    await _recheck_for_file(file_id, list(image_text_service.IMAGE_KINDS), "the text in its images")


async def recheck_after_document_text(file_id: UUID) -> None:
    """When a script's (or article's) text has been read (or couldn't be), re-run the check so the score includes it."""
    await _recheck_for_file(file_id, ["SCRIPT", "MAIN", "SUPPORTING"], "the script")


async def _recheck_for_file(file_id: UUID, kinds: List[str], what: str) -> None:
    """Re-run the check of the open submission a file belongs to, after something about the file was read."""
    try:
        sub = await fetch_one(
            """
            SELECT s.* FROM content_submissions s
            JOIN content_submission_files f ON f.submission_id = s.id
            WHERE f.id = $1 AND f.kind = ANY($2::text[]) AND f.deleted_at IS NULL
              AND s.status IN ('DRAFT', 'IN_REVIEW', 'CHANGES_REQUESTED') AND s.deleted_at IS NULL
            """,
            file_id, kinds,
        )
        if not sub:
            return
        result, check = await _run_check(sub)
        # Tell the creator the first time the video turns out not to match their approved script.
        was_mismatch = ((_json(sub["latest_check"]) or {}).get("script_match") or {}).get("status") == "MISMATCH"
        newly_mismatched = ((check.get("script_match") or {}).get("status") == "MISMATCH") and not was_mismatch
        pool = get_pool()
        async with pool.acquire() as conn:
            async with conn.transaction():
                await conn.execute(
                    "UPDATE content_submissions SET latest_check = $2::jsonb WHERE id = $1",
                    sub["id"], json.dumps(check),
                )
                await _mirror_to_script_version(conn, sub["id"], check)
                await conn.execute(
                    """
                    UPDATE content_review_jobs
                    SET automated_check = $3::jsonb, score = $4, traffic_light = $5
                    WHERE submission_id = $1 AND version_number = $2 AND status = 'AWAITING_REVIEW'
                    """,
                    sub["id"], sub["current_version"], json.dumps(check), result.score, result.traffic_light,
                )
                summary = (
                    f"Read {what}; still reading the rest of the post before scoring"
                    if analysis_pending(check)
                    else f"Re-checked with {what}: the AI review couldn't run, so there is no score yet" if check["ai_unavailable"]
                    else f"Re-checked with {what}: {result.violations_count} issue(s), score {result.score}/100"
                )
                await _add_event(conn, sub["id"], "CHECK_COMPLETED", summary, None,
                                 {"traffic_light": result.traffic_light, "score": result.score})
        if newly_mismatched:
            await _notify(
                [sub["created_by"]],
                f"{sub['reference_code']}: the video doesn't match your approved script",
                f"\"{sub['title']}\" says something quite different from the script a reviewer approved "
                f"({check['script_match']['similarity']}% alike). Check you uploaded the right video before sending it for review.",
                f"/submissions/{sub['id']}",
                sub["id"],
            )
    except Exception:
        logger.exception("Couldn't re-check submission after reading %s of %s", what, file_id)
