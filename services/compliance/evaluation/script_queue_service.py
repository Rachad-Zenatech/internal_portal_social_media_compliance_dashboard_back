"""Queue membership shared by script lists and their tab counts."""
import asyncio
from uuid import UUID
from models.compliance.script_queue_model import ScriptQueueSummary
from postgresql_db.database import fetch_all
from services.compliance.submissions import submission_service as subs

QUEUE_TABS = {
    "review": ("s.script_status = 'IN_REVIEW'", "v.created_at"),
    "sent_back": ("s.script_status IN ('CHANGES_REQUESTED', 'EDITS_PROPOSED')", "s.updated_at DESC"),
    "done": ("s.script_status = 'APPROVED' AND s.status = 'DRAFT' AND s.updated_at > NOW() - INTERVAL '30 days'",
             "s.updated_at DESC"),
}


async def summary(user_id: UUID) -> list[ScriptQueueSummary]:
    """Read all tabs' membership without starting compliance checks or fetching script bodies."""
    team, brands = await asyncio.gather(subs._can_see_team_content(user_id), subs._brand_scope(user_id))
    if not team:
        return []
    membership = " OR ".join(f"({where})" for where, _ in QUEUE_TABS.values())
    rows = await fetch_all(f"""
        SELECT s.id, s.script_status, bi.batch_id
        FROM content_submissions s
        LEFT JOIN content_script_batch_items bi ON bi.submission_id = s.id
        WHERE ({membership}) AND s.script_required AND s.content_type = 'VIDEO' AND s.deleted_at IS NULL
          AND EXISTS (SELECT 1 FROM content_script_versions sv WHERE sv.submission_id = s.id)
          AND (s.created_by = $2 OR {subs._reviewer_sees("$2", "$1")})
        ORDER BY s.updated_at DESC LIMIT 2000
        """, brands, user_id)
    return [ScriptQueueSummary(**row) for row in rows]
