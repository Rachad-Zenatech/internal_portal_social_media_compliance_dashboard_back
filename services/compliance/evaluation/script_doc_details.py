"""Shared or independent post details for the scripts in a document."""
import json

from postgresql_db.database import fetch_one, get_pool


async def shared(doc: dict) -> bool:
    if not doc.get("set_id"):
        return True
    row = await fetch_one("SELECT shared_details FROM script_sets WHERE id = $1", doc["set_id"])
    return row.get("shared_details", True) if row else True


async def save(doc: dict, details, same_for_all: bool | None) -> None:
    async with get_pool().acquire() as conn:
        async with conn.transaction():
            settings = await conn.fetchrow("SELECT shared_details FROM script_sets WHERE id = $1 FOR UPDATE", doc.get("set_id")) if doc.get("set_id") else None
            use_shared = same_for_all if same_for_all is not None else (settings.get("shared_details", True) if settings else True)
            if settings and same_for_all is not None:
                await conn.execute("UPDATE script_sets SET shared_details = $2, updated_at = NOW() WHERE id = $1", doc["set_id"], use_shared)
            if details is None and not (same_for_all is True and settings):
                return
            # Turning sharing back on uses the current script's details.
            raw = doc.get("details") or {}
            current = json.loads(raw) if isinstance(raw, str) else raw
            payload = details.model_dump(mode="json", exclude_none=True) if details is not None else current
            payload = {key: value for key, value in payload.items() if key != "source_submission_id"}
            encoded = json.dumps(payload)
            # Preserve each script's own link to its originating post.
            assignment = "details = jsonb_strip_nulls(jsonb_build_object('source_submission_id', details->'source_submission_id')) || $2::jsonb, updated_at = NOW()"
            if use_shared and settings:
                await conn.execute("UPDATE script_sets SET details = $2::jsonb, updated_at = NOW() WHERE id = $1", doc["set_id"], encoded)
                await conn.execute(f"UPDATE script_documents SET {assignment} WHERE set_id = $1 AND deleted_at IS NULL AND status IN ('DRAFT', 'CHANGES_REQUESTED')", doc["set_id"], encoded)
            else:
                await conn.execute(f"UPDATE script_documents SET {assignment} WHERE id = $1", doc["id"], encoded)
