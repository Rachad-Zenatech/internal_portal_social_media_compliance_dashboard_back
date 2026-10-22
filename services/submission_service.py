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
from services.audit_service import log_audit_event
from services import ai_usage_service, approval_path_service, canva_service, review_deadline_service, transcription_service
from services.ai_usage_service import AiLimitExceeded
from services.compliance_service import check_compliance, flag_segments

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
    "AD": IMAGE_EXT | VIDEO_EXT,
}
# Types whose main asset is required to submit; posts and articles can be text-only. An ad needs its creative.
MAIN_FILE_REQUIRED = {"VIDEO", "IMAGE", "CAROUSEL", "AD"}


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
    from services.auth_service import user_can_access_page
    return await _is_super_admin(user_id) or await user_can_access_page(user_id, "CONTENT_SUBMISSIONS", "DELETE")


async def _can_review(user_id: UUID) -> bool:
    """Reviewers (the people who publish): "Review & Publish Content" (Content Reviewer, Content Manager,
    Administrator) and super admins."""
    from services.auth_service import user_can_access_page
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
        if scope is not None and row["brand_code"] not in scope:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Submission not found")
    return row


def _approvers(rules: "approval_path_service.ApprovalRules", row: Dict[str, Any], accounts=None) -> Tuple[List[UUID], List[str]]:
    """The named final approvers for a submission row (empty: any reviewer on its brand)."""
    accts = accounts if accounts is not None else [_account_option(row, prefix="acc_")]
    ceo = approval_path_service.is_ceo_content(row.get("rule_sets") or [], accts)
    return rules.approvers(row.get("brand_code"), row.get("audience"), ceo)


def _may_approve(user_id: UUID, is_super: bool, approver_ids: List[UUID]) -> bool:
    return is_super or not approver_ids or user_id in approver_ids


def _approval_fields(rules, r: Dict[str, Any], user_id: UUID, is_reviewer: bool, is_super: bool, accounts=None) -> Dict[str, Any]:
    ids, names = _approvers(rules, r, accounts)
    can_review = r["status"] == "IN_REVIEW" and is_reviewer and (r["created_by"] != user_id or is_super)
    return {"approver_names": names, "can_approve": can_review and _may_approve(user_id, is_super, ids)}


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
               transcript, transcript_status, transcript_error,
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
    return {"due_at": None, "due_kind": None}


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
        account_ids=_account_ids(row),
        accounts=_accounts(row),
        publish_at=row["publish_at"],
        **_due(row),
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


async def list_submissions(user_id: UUID, status_filter: Optional[str] = None, scope: str = "mine") -> List[SubmissionSummary]:
    """The user's own submissions, or everyone's (scope="all") for super admins, Content Managers and reviewers."""
    is_manager = await _can_manage_team_content(user_id)
    is_reviewer = await _can_review(user_id)
    is_super = await _is_super_admin(user_id)
    if scope == "all" and not (is_manager or is_reviewer):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="You can only see your own content")
    rows = await fetch_all(
        """
        SELECT s.id, s.reference_code, s.title, s.content_type, s.brand_code, s.platforms, s.status,
               s.current_version, s.publish_at, s.submitted_at, s.created_at, s.updated_at, s.audience,
               s.due_at, s.due_kind, s.rule_sets,
               left(s.caption_text, 600) AS caption_text,
               s.latest_check->>'traffic_light' AS latest_traffic_light,
               (s.latest_check->>'score')::int AS latest_score,
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
        WHERE (s.created_by = $1 OR ($3 AND ($4::text[] IS NULL OR s.brand_code = ANY($4::text[]))))
          AND s.deleted_at IS NULL AND ($2::text IS NULL OR s.status = $2)
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
        scope == "all",
        # Everyone else's content only for the brands the viewer works on.
        await _brand_scope(user_id),
    )
    rules = await approval_path_service.load_rules()
    summaries = []
    for r in rows:
        base = {k: v for k, v in r.items()
                if not k.startswith(("acc_", "cov_"))
                and k not in ("platforms", "accounts_json", "due_at", "due_kind", "rule_sets")}
        cover = {k[4:]: v for k, v in r.items() if k.startswith("cov_")}
        summaries.append(SubmissionSummary(
            **base,
            platforms=list(r["platforms"] or []),
            **_due(r),
            account=_account_option(r, prefix="acc_"),
            accounts=_accounts(r),
            cover=_file_out(r["id"], cover) if cover["id"] else None,
            can_delete=r["status"] == "DRAFT" and (r["created_by"] == user_id or is_manager),
            can_review=r["status"] == "IN_REVIEW" and is_reviewer and (r["created_by"] != user_id or is_super),
            **_approval_fields(rules, r, user_id, is_reviewer, is_super, _accounts(r)),
            can_manage_approval=r["status"] == "APPROVED" and is_reviewer and (r["created_by"] != user_id or is_super),
        ))
    return summaries


async def get_submission(submission_id: UUID, user_id: UUID) -> Submission:
    row = await _get_owned_row(submission_id, user_id, manager_can_read=True)
    sub = await _to_submission(row)
    is_creator = row["created_by"] == user_id
    # Managers can read and delete someone else's draft but not edit it.
    sub.can_edit = sub.can_edit and (is_creator or await _is_super_admin(user_id))
    sub.can_delete = row["status"] == "DRAFT" and (is_creator or await _can_manage_team_content(user_id))
    approver_ids, sub.approver_names = _approvers(await approval_path_service.load_rules(), row, _accounts(row))
    if row["status"] in ("IN_REVIEW", "APPROVED"):
        is_super = await _is_super_admin(user_id)
        may_decide = await _can_review(user_id) and (not is_creator or is_super)
        sub.can_review = row["status"] == "IN_REVIEW" and may_decide
        sub.can_approve = sub.can_review and _may_approve(user_id, is_super, approver_ids)
        sub.can_manage_approval = row["status"] == "APPROVED" and may_decide
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
    if payload.get("brand_code"):
        brand = await fetch_one("SELECT code FROM brands WHERE code = $1 AND is_active", payload["brand_code"])
        if not brand:
            raise HTTPException(status_code=422, detail="Unknown brand")
        # Only checked when it changes, so an older draft on a brand someone has since left still saves.
        if brand["code"] != row["brand_code"]:
            scope = await _brand_scope(user_id)
            if scope is not None and brand["code"] not in scope:
                raise HTTPException(status_code=403, detail="You don't work on that brand")
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
        if name == "account_id":
            fields["account_ids"] = []

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
    """Delete a draft. Allowed for its creator, super admins and Content Managers.
    A draft that was never submitted is removed with its files; one withdrawn after review is hidden instead,
    so its versions, review jobs and activity stay on record."""
    row = await _get_owned_row(submission_id, user_id, manager_can_read=True)
    if row["status"] != "DRAFT":
        raise HTTPException(status_code=409, detail="Only drafts can be deleted. Withdraw it from review first.")
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
            from services.notification_service import create_notification
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
    return await _record_file(row, kind, file_id, path, original_name, mime, size, user_id)


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
            await conn.execute("UPDATE content_submissions SET updated_at = NOW(), updated_by = $2 WHERE id = $1",
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
    return _file_out(submission_id, dict(saved))


# ---------------------------------------------------------------------------
# Canva import
# ---------------------------------------------------------------------------

async def _store_canva_export(row: Dict[str, Any], export: canva_service.DesignExport, user_id: UUID,
                              replaces_file_id: Optional[UUID] = None) -> SubmissionFile:
    """Write a Canva export into the submission's storage and record it as the main file."""
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

def _check_text(row: Dict[str, Any]) -> str:
    return "\n\n".join(p for p in [row["title"], row["caption_text"]] if p and p.strip())


async def _run_check(row: Dict[str, Any]) -> Tuple[Any, Dict[str, Any]]:
    """The one rulebook check every screen shows: title, caption and (once it exists) the video transcript,
    against the rules scoped to this submission. Includes per-segment flags for the reviewer's timeline."""
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
    text = "\n\n".join(p for p in [_check_text(row), spoken] if p) or row["title"]
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
    check = {
        **result.model_dump(mode="json"),
        "checked_at": datetime.now(timezone.utc).isoformat(),
        "includes_transcript": bool(segments),
        # A video whose transcript isn't ready yet is re-checked automatically when it is.
        "transcript_pending": bool(video) and video["transcript_status"] not in ("COMPLETED", "FAILED"),
        "segment_flags": flag_segments(segments, result.violations),
    }
    return result, check


async def run_initial_check(submission_id: UUID, user_id: UUID) -> Submission:
    row = await _get_owned_row(submission_id, user_id)
    if not _check_text(row):
        raise HTTPException(status_code=422, detail="Add a title or caption before running the check")
    result, check = await _run_check(row)
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
    if not _account_ids(row):
        missing.append("account_id")
    if not row["rule_sets"]:
        missing.append("rule_sets")
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
        decision=r.get("decision"),
        decision_notes=r.get("decision_notes"),
        decided_by_name=r.get("decided_by_name"),
        decided_at=r.get("decided_at"),
    )


async def submit_for_review(submission_id: UUID, user_id: UUID) -> Tuple[Submission, ReviewJob]:
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
    if row["publish_at"] and row["publish_at"] < datetime.now(timezone.utc):
        missing.append("publish_at")
    if missing:
        raise HTTPException(status_code=422, detail={
            "message": "Some required information is missing.",
            "missing": sorted(set(missing)),
        })

    # Automated rulebook screening is the first step of the review job.
    result, check = await _run_check(row)

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
        "accounts": [a.model_dump(mode="json") for a in accounts],
        "publish_at": row["publish_at"].isoformat() if row["publish_at"] else None,
        "caption_html": row["caption_html"],
        "caption_text": row["caption_text"],
        "rule_sets": list(row["rule_sets"] or []),
        "reviewer_route": list(row["reviewer_route"] or []),
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
                    due_at = $5, due_kind = 'REVIEW', due_reminded_at = NULL, due_overdue_notified_at = NULL
                WHERE id = $1
                """,
                submission_id, version, json.dumps(check), user_id,
                review_deadline_service.due_date("REVIEW", row["publish_at"]),
            )
            await _add_event(conn, submission_id, "SUBMITTED", f"Version {version} submitted for review", user_id,
                             {"version": version, "review_job_id": str(job["id"])})
            await _add_event(conn, submission_id, "CHECK_COMPLETED",
                             f"Automated rulebook check: {result.violations_count} issue(s), score {result.score}/100",
                             user_id, {"traffic_light": result.traffic_light, "score": result.score})
            await _add_event(conn, submission_id, "REVIEW_REQUESTED", "Sent to the review queue", user_id)

    await log_audit_event("content_submission", str(submission_id), "SUBMIT_FOR_REVIEW", user_id,
                          {"reference_code": row["reference_code"], "version": version,
                           "traffic_light": result.traffic_light, "score": result.score})
    try:
        from services.notification_service import create_notification
        await create_notification(NotificationCreate(
            user_id=str(user_id),
            type="SUBMISSION",
            title=f"{row['reference_code']} submitted for review",
            message=f"\"{row['title']}\" (version {version}) is in the review queue. "
                    f"Automated check: {result.score}/100.",
            link_url=f"/submissions/{submission_id}",
            entity_type="content_submission",
            entity_id=str(submission_id),
        ))
    except Exception:
        logger.warning("Could not create submission notification", exc_info=True)
    await _notify_reviewers(row, version, result.score, user_id)
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


async def review_queue_counts(user_id: UUID) -> ReviewQueueCounts:
    """How many videos are in each Screener tab, for the tab labels."""
    see_all = await _can_see_team_content(user_id)
    row = await fetch_one(
        f"""
        SELECT
            count(*) FILTER (WHERE {_queue_filter("review")}) AS review,
            count(*) FILTER (WHERE {_queue_filter("sent_back")}) AS sent_back,
            count(*) FILTER (WHERE {_queue_filter("done")}) AS done
        FROM content_submissions s
        WHERE s.deleted_at IS NULL
          AND (s.created_by = $1 OR ($2 AND ($3::text[] IS NULL OR s.brand_code = ANY($3::text[]))))
          AND EXISTS (SELECT 1 FROM content_submission_files cf
                      WHERE cf.submission_id = s.id AND cf.kind = 'MAIN' AND cf.deleted_at IS NULL
                        AND cf.mime_type LIKE 'video/%')
        """,
        user_id,
        see_all,
        await _brand_scope(user_id),
    )
    return ReviewQueueCounts(**dict(row))


async def review_queue(user_id: UUID, tab: str = "review", search: Optional[str] = None) -> List[ReviewQueueItem]:
    """Submitted videos for one Content Screener tab: everyone's for reviewers, Content Managers and super admins,
    otherwise the user's own. Done covers the last 30 days and can be searched by title or reference."""
    if tab not in QUEUE_TAB_STATUSES:
        raise HTTPException(status_code=422, detail="Unknown queue tab")
    see_all = await _can_see_team_content(user_id)
    is_reviewer = await _can_review(user_id)
    is_super = await _is_super_admin(user_id)
    term = (search or "").strip()
    rows = await fetch_all(
        f"""
        SELECT s.id, s.reference_code, s.title, s.status, s.platforms, s.caption_text, s.latest_check,
               s.current_version, s.submitted_at, s.created_by, s.publish_at, s.due_at, s.due_kind,
               s.brand_code, s.audience, s.rule_sets,
               {_DECIDED_AT} AS decided_at,
               u.full_name AS creator_name,
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
        WHERE {_queue_filter(tab)} AND s.deleted_at IS NULL
          AND (s.created_by = $1 OR ($2 AND ($3::text[] IS NULL OR s.brand_code = ANY($3::text[]))))
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
            can_review=r["status"] == "IN_REVIEW" and is_reviewer and (r["created_by"] != user_id or is_super),
            **_approval_fields(rules, r, user_id, is_reviewer, is_super),
            publish_at=r["publish_at"],
            **_due(r),
            created_by=r["created_by"],
            can_manage_approval=r["status"] == "APPROVED" and is_reviewer and (r["created_by"] != user_id or is_super),
            decided_at=r["decided_at"],
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


async def _reviewer_ids(exclude: Optional[UUID] = None, brand_code: Optional[str] = None) -> List[UUID]:
    """Active users who can review: super admins and roles with "Review & Publish Content". With a brand, only
    reviewers who work on it (or on every brand)."""
    rows = await fetch_all(
        """
        SELECT u.id FROM users u
        WHERE u.is_active AND u.deleted_at IS NULL AND ($1::uuid IS NULL OR u.id <> $1)
          AND (
            $3::text IS NULL OR u.is_super_admin
            OR NOT EXISTS (SELECT 1 FROM user_brands ub WHERE ub.user_id = u.id)
            OR EXISTS (SELECT 1 FROM user_brands ub WHERE ub.user_id = u.id AND ub.brand_code = $3)
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
        exclude, REVIEW_GROUP, brand_code,
    )
    return [r["id"] for r in rows]


async def _notify(user_ids: List[UUID], title: str, message: str, link: str, submission_id: UUID) -> None:
    if not user_ids:
        return
    try:
        import asyncio
        from services.notification_service import create_notification
        await asyncio.gather(*(
            create_notification(NotificationCreate(
                user_id=str(uid), type="SUBMISSION", title=title, message=message, link_url=link,
                entity_type="content_submission", entity_id=str(submission_id),
            ))
            for uid in user_ids
        ))
    except Exception:
        logger.warning("Could not send review notifications for %s", submission_id, exc_info=True)


async def _notify_reviewers(row: Dict[str, Any], version: int, score: int, submitter: UUID) -> None:
    author = await fetch_one("SELECT full_name FROM users WHERE id = $1", submitter)
    again = " (resubmitted)" if version > 1 else ""
    who = author["full_name"] if author else "Someone"
    reviewers = await _reviewer_ids(exclude=submitter, brand_code=row["brand_code"])
    approver_ids, _ = _approvers(await approval_path_service.load_rules(), row, _accounts(row))
    approvers = [a for a in approver_ids if a != submitter]
    await _notify(
        approvers,
        f"{row['reference_code']} needs your approval{again}",
        f"{who} sent \"{row['title']}\" (version {version}) for review, and you give final approval for it. "
        f"Automated check: {score}/100.",
        f"/submissions/{row['id']}",
        row["id"],
    )
    await _notify(
        [r for r in reviewers if r not in approvers],
        f"{row['reference_code']} is ready for review{again}",
        f"{who} sent \"{row['title']}\" (version {version}) for review. Automated check: {score}/100.",
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
    if req.decision == "APPROVE":
        approver_ids, approver_names = _approvers(await approval_path_service.load_rules(), row, _accounts(row))
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
                    completed_at = NOW()
                WHERE submission_id = $1 AND version_number = $2 AND status = 'AWAITING_REVIEW'
                """,
                submission_id, version, new_status, req.decision, notes, user_id,
            )
            revision_due = review_deadline_service.due_date("REVISION", publish_at) if new_status == "CHANGES_REQUESTED" else None
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
                              "publish_at": publish_at.isoformat() if publish_at else None,
                              "publish_now": req.publish_now})

    await log_audit_event("content_submission", str(submission_id), f"REVIEW_{req.decision}", user_id,
                          {"reference_code": row["reference_code"], "version": version, "notes": notes,
                           "publish_at": publish_at.isoformat() if publish_at else None})

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
                submission_id, user_id, review_deadline_service.due_date("REVISION", row["publish_at"]),
            )
            await _add_event(conn, submission_id, "APPROVAL_REOPENED",
                             f"Approval of version {version} reopened: changes requested", user_id,
                             {"version": version, "notes": notes,
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
        f"{reviewer['full_name'] if reviewer else 'A reviewer'} reopened the approval of \"{row['title']}\": \"{notes}\"",
        f"/submissions/{submission_id}",
        submission_id,
    )
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


async def recheck_after_transcript(file_id: UUID) -> None:
    """When a video's transcript is ready, re-run the check so the score includes what's said and shown.
    Approved and withdrawn items keep the check they were decided on."""
    try:
        sub = await fetch_one(
            """
            SELECT s.* FROM content_submissions s
            JOIN content_submission_files f ON f.submission_id = s.id
            WHERE f.id = $1 AND f.kind = 'MAIN' AND f.deleted_at IS NULL
              AND s.status IN ('DRAFT', 'IN_REVIEW', 'CHANGES_REQUESTED') AND s.deleted_at IS NULL
            """,
            file_id,
        )
        if not sub:
            return
        result, check = await _run_check(sub)
        pool = get_pool()
        async with pool.acquire() as conn:
            async with conn.transaction():
                await conn.execute(
                    "UPDATE content_submissions SET latest_check = $2::jsonb WHERE id = $1",
                    sub["id"], json.dumps(check),
                )
                await conn.execute(
                    """
                    UPDATE content_review_jobs
                    SET automated_check = $3::jsonb, score = $4, traffic_light = $5
                    WHERE submission_id = $1 AND version_number = $2 AND status = 'AWAITING_REVIEW'
                    """,
                    sub["id"], sub["current_version"], json.dumps(check), result.score, result.traffic_light,
                )
                await _add_event(conn, sub["id"], "CHECK_COMPLETED",
                                 f"Re-checked with the video transcript: {result.violations_count} issue(s), "
                                 f"score {result.score}/100", None,
                                 {"traffic_light": result.traffic_light, "score": result.score})
    except Exception:
        logger.exception("Couldn't re-check submission after transcription of %s", file_id)
