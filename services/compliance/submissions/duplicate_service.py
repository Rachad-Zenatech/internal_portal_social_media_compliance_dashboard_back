"""
Duplicate Post Check
A post is the same as one already sent for approval when its main files (by their bytes) and its caption match. The
same post can go out again on other platforms, accounts, brands, audiences or regions: it's only a duplicate when every
one of those overlaps with the earlier post. Then the publish date decides: the same date is a duplicate and can't be
submitted; another date is a repost, which the creator confirms.

Scripts (the script stage of video posts) follow the same rules: a script is the same as one already sent for review
when its text matches (ignoring case and spacing), and the submission details and publish date decide as for posts.
"""

import asyncio
import hashlib
import logging
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple
from uuid import UUID

from models.compliance.submission_model import AUDIENCES, DuplicateDetail, DuplicateMatch, DuplicateReport
from postgresql_db.database import fetch_all, get_pool
from services.compliance.evaluation.compliance_service import audiences_of, brands_of

logger = logging.getLogger(__name__)

# The Submission Details dropdowns compared, in the order the form shows them.
DETAIL_FIELDS: Tuple[Tuple[str, str], ...] = (
    ("brands", "Brand(s)"),
    ("accounts", "Publishing Account(s)"),
    ("audiences", "Target Audience(s)"),
    ("region", "Region"),
    ("platforms", "Platform(s)"),
)

# Posts that have been sent for approval (drafts haven't).
SENT_STATUSES = ("IN_REVIEW", "CHANGES_REQUESTED", "APPROVED", "REJECTED")

_PLATFORM_LABELS = {"TIKTOK": "TikTok", "YOUTUBE": "YouTube", "YOUTUBE_SHORTS": "YouTube Shorts", "LINKEDIN": "LinkedIn",
                    "INSTAGRAM": "Instagram", "FACEBOOK": "Facebook", "X": "X", "REDDIT": "Reddit", "WEBSITE": "Website"}


def normalize_caption(text: Optional[str]) -> str:
    """Captions compare without case or spacing differences."""
    return re.sub(r"\s+", " ", (text or "").strip()).lower()


def details_of(row: Dict[str, Any]) -> Dict[str, List[str]]:
    """The post's Submission Details values, as keys (brand codes, account ids, audience codes...)."""
    accounts = list(row.get("account_ids") or ([row["account_id"]] if row.get("account_id") else []))
    return {
        "brands": [str(b) for b in brands_of(row)],
        "accounts": [str(a) for a in accounts],
        "audiences": [str(a) for a in audiences_of(row)],
        "region": [row["region"]] if row.get("region") else [],
        "platforms": [str(p) for p in row.get("platforms") or []],
    }


def compare_details(mine: Dict[str, List[str]], theirs: Dict[str, List[str]]) -> Tuple[Dict[str, List[str]], Dict[str, List[str]], bool]:
    """Per dropdown: the values both posts have, and the values only this post has. The posts overlap completely when
    every dropdown shares at least one value (then the same post would go out to the same place again)."""
    shared: Dict[str, List[str]] = {}
    new: Dict[str, List[str]] = {}
    for field, _ in DETAIL_FIELDS:
        theirs_set = set(theirs.get(field) or [])
        shared[field] = [v for v in mine.get(field) or [] if v in theirs_set]
        new[field] = [v for v in mine.get(field) or [] if v not in theirs_set]
    return shared, new, all(shared[f] for f, _ in DETAIL_FIELDS)


def same_publish_date(a: Optional[datetime], b: Optional[datetime]) -> bool:
    """Same calendar day (UTC), or neither has a date."""
    if a is None or b is None:
        return a is None and b is None
    return a.astimezone(timezone.utc).date() == b.astimezone(timezone.utc).date()


def _sha256(path: Path) -> Optional[str]:
    try:
        digest = hashlib.sha256()
        with open(path, "rb") as f:
            while chunk := f.read(1024 * 1024):
                digest.update(chunk)
        return digest.hexdigest()
    except OSError:
        logger.warning("Couldn't read %s to compare it", path)
        return None


async def _content_hashes(submission_ids: Sequence[UUID]) -> Dict[UUID, List[str]]:
    """Each post's main files as sorted content hashes. A file without a saved hash is hashed now and the hash kept."""
    from services.compliance.submissions.submission_service import STORAGE_ROOT

    rows = await fetch_all(
        """
        SELECT id, submission_id, storage_path, content_sha256 FROM content_submission_files
        WHERE submission_id = ANY($1::uuid[]) AND kind = 'MAIN' AND deleted_at IS NULL
        """,
        list(submission_ids),
    )
    out: Dict[UUID, List[str]] = {sid: [] for sid in submission_ids}
    fresh: List[Tuple[str, UUID]] = []
    for r in rows:
        digest = (r["content_sha256"] or "").strip() or None
        if not digest:
            digest = await asyncio.to_thread(_sha256, STORAGE_ROOT / r["storage_path"])
            if digest:
                fresh.append((digest, r["id"]))
        # A file that can't be read can't match anything.
        out[r["submission_id"]].append(digest or f"unreadable:{r['id']}")
    if fresh:
        async with get_pool().acquire() as conn:
            await conn.executemany("UPDATE content_submission_files SET content_sha256 = $1 WHERE id = $2", fresh)
    return {sid: sorted(h) for sid, h in out.items()}


async def _labels(rows: Sequence[Dict[str, Any]]) -> Dict[str, Dict[str, str]]:
    """Display names for the dropdown values, by field."""
    from services.compliance.submissions.submission_service import _accounts

    brand_codes = sorted({b for r in rows for b in brands_of(r)})
    brands = await fetch_all("SELECT code, name FROM brands WHERE code = ANY($1::text[])", brand_codes) if brand_codes else []
    accounts: Dict[str, str] = {}
    for r in rows:
        for a in _accounts(r):
            accounts[str(a.id)] = a.label
    return {
        "brands": {b["code"]: b["name"] for b in brands},
        "accounts": accounts,
        "audiences": dict(AUDIENCES),
        "region": {},
        "platforms": dict(_PLATFORM_LABELS),
    }


async def find(row: Dict[str, Any]) -> DuplicateReport:
    """Posts already sent for approval with the same content and caption as this one, and how their details overlap."""
    from services.compliance.submissions.submission_service import _ACCOUNTS_COLUMN

    caption = normalize_caption(row.get("caption_text"))
    if not caption:
        return DuplicateReport()
    candidates = await fetch_all(
        """
        SELECT s.*, u.full_name AS created_by_name,"""
        + _ACCOUNTS_COLUMN
        + """
        FROM content_submissions s
        LEFT JOIN users u ON u.id = s.created_by
        WHERE s.deleted_at IS NULL AND s.id <> $1 AND s.status = ANY($2::text[])
          AND lower(regexp_replace(btrim(coalesce(s.caption_text, '')), '\\s+', ' ', 'g')) = $3
        ORDER BY s.submitted_at DESC NULLS LAST
        LIMIT 50
        """,
        row["id"], list(SENT_STATUSES), caption,
    )
    if not candidates:
        return DuplicateReport()
    hashes = await _content_hashes([row["id"], *(c["id"] for c in candidates)])
    same = [c for c in candidates if hashes.get(c["id"]) == hashes.get(row["id"])]
    if not same:
        return DuplicateReport()

    return await _report(row, same)


async def _report(row: Dict[str, Any], same: List[Any], sent_key: str = "submitted_at") -> DuplicateReport:
    """How each matching post's details overlap with this one's, and whether that blocks it or asks for a repost."""
    labels = await _labels([row, *same])
    mine = details_of(row)
    matches: List[DuplicateMatch] = []
    for c in same:
        shared, new, overlaps = compare_details(mine, details_of(c))

        def named(field: str, values: List[str]) -> List[str]:
            return [labels[field].get(v, v.replace("_", " ").title() if field != "region" else v) for v in values]

        matches.append(DuplicateMatch(
            submission_id=c["id"],
            reference_code=c["reference_code"],
            title=c["title"],
            status=c["status"],
            submitted_at=c.get(sent_key),
            publish_at=c.get("publish_at"),
            created_by_name=c.get("created_by_name"),
            details=[
                DuplicateDetail(field=f, label=label, shared=named(f, shared[f]), new=named(f, new[f]))
                for f, label in DETAIL_FIELDS
            ],
            all_overlap=overlaps,
            same_date=same_publish_date(row.get("publish_at"), c.get("publish_at")),
        ))
    blocking = next((m for m in matches if m.all_overlap and m.same_date), None)
    repost = None if blocking else next((m for m in matches if m.all_overlap), None)
    return DuplicateReport(matches=matches, blocked=blocking is not None, blocking=blocking, repost=repost)


# Scripts that have been sent for review (a draft script hasn't).
SCRIPT_SENT_STATUSES = ("IN_REVIEW", "CHANGES_REQUESTED", "EDITS_PROPOSED", "APPROVED")


def normalize_script(text: Optional[str]) -> str:
    """A script's text as compared: case and spacing (line breaks included) don't make it different."""
    return re.sub(r"\s+", " ", (text or "").strip()).lower()


async def find_script(row: Dict[str, Any], body: str) -> DuplicateReport:
    """Other posts whose script, as last sent for review, is the same text as this one, and how their details overlap
    (the same rules as posts: every detail overlapping and the same publish date is a duplicate; another date a repost)."""
    from services.compliance.submissions.submission_service import _ACCOUNTS_COLUMN

    text = normalize_script(body)
    if not text:
        return DuplicateReport()
    same = await fetch_all(
        """
        SELECT s.*, u.full_name AS created_by_name, v.created_at AS script_sent_at,"""
        + _ACCOUNTS_COLUMN
        + """
        FROM content_submissions s
        JOIN LATERAL (
            SELECT body, created_at FROM content_script_versions sv
            WHERE sv.submission_id = s.id ORDER BY version_number DESC LIMIT 1
        ) v ON true
        LEFT JOIN users u ON u.id = s.created_by
        WHERE s.deleted_at IS NULL AND s.id <> $1 AND s.script_status = ANY($2::text[])
          AND lower(regexp_replace(btrim(v.body), '\\s+', ' ', 'g')) = $3
        ORDER BY v.created_at DESC
        LIMIT 50
        """,
        row["id"], list(SCRIPT_SENT_STATUSES), text,
    )
    if not same:
        return DuplicateReport()
    return await _report(row, [dict(c) for c in same], sent_key="script_sent_at")
