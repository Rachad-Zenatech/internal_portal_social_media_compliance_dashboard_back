"""Submitted Create Post scripts projected into the script hub without a second review workflow."""

from typing import List
from uuid import UUID

from models.compliance.script_doc_model import DocSummary
from postgresql_db.database import fetch_all
from services.compliance.submissions import submission_service as subs


async def list_submission_scripts(user_id: UUID, scope: str) -> List[DocSummary]:
    """Creator's own unlinked uploads in My scripts; assigned uploads in Sent to me; permitted uploads in All scripts."""
    if scope not in ("mine", "review", "all"):
        return []
    if scope == "mine":
        is_super = await subs._is_super_admin(user_id)
        can_review = await subs._can_review(user_id)
        visibility, args = "s.created_by = $1", [user_id]
    else:
        is_super = await subs._is_super_admin(user_id)
        if not is_super and not await subs._can_see_team_content(user_id):
            return []
        can_review = await subs._can_review(user_id)
        if scope == "review":
            visibility, args = "$1::uuid = ANY(s.reviewer_ids)", [user_id]
        elif is_super:
            visibility, args = "TRUE", []
        else:
            visibility = f"(s.created_by = $1 OR {subs._reviewer_sees('$1', '$2')})"
            args = [user_id, await subs._brand_scope(user_id)]
    rows = await fetch_all(
        f"""
        SELECT s.id, s.title, s.reference_code, s.created_by, s.script_status, s.updated_at, s.created_at,
               coalesce(nullif(u.full_name, ''), u.email) AS creator_name,
               coalesce((SELECT array_agg(coalesce(nullif(ru.full_name, ''), ru.email) ORDER BY ru.full_name)
                         FROM users ru WHERE ru.id = ANY(s.reviewer_ids)), '{{}}') AS reviewer_names,
               v.version_number
        FROM content_submissions s
        JOIN LATERAL (
            SELECT version_number FROM content_script_versions sv
            WHERE sv.submission_id = s.id ORDER BY version_number DESC LIMIT 1
        ) v ON true
        LEFT JOIN users u ON u.id = s.created_by
        WHERE s.deleted_at IS NULL AND s.script_required AND s.content_type = 'VIDEO'
          AND s.script_status IN ('IN_REVIEW', 'CHANGES_REQUESTED', 'EDITS_PROPOSED', 'APPROVED')
          AND ({visibility})
          AND NOT EXISTS (
              SELECT 1 FROM script_document_posts p JOIN script_documents d ON d.id = p.document_id
              WHERE p.submission_id = s.id AND d.deleted_at IS NULL
          )
        ORDER BY s.updated_at DESC, s.id LIMIT 300
        """, *args)
    return [_summary(row, user_id, can_review, is_super) for row in rows]


def _summary(row, user_id: UUID, can_review: bool, is_super: bool) -> DocSummary:
    own = row["created_by"] == user_id
    source_status = row["script_status"]
    needs = (source_status == "IN_REVIEW" and can_review and (not own or is_super)) or (
        own and source_status in ("CHANGES_REQUESTED", "EDITS_PROPOSED"))
    return DocSummary(
        id=row["id"], submission_id=row["id"], reference_code=row["reference_code"], source_status=source_status,
        title=row["title"], template_key="uploaded", status="CHANGES_REQUESTED" if source_status == "EDITS_PROPOSED" else source_status,
        round=row["version_number"], creator_id=row["created_by"], creator_name=row["creator_name"] or "Unknown creator",
        reviewer_names=list(row["reviewer_names"] or []), needs_you=bool(needs),
        updated_at=row["updated_at"], created_at=row["created_at"],
    )
