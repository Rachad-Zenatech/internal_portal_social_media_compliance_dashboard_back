"""Record exact input-hash reuse separately from the original model run's time."""
import json

from postgresql_db.database import fetch_one
from services.compliance.submissions import media_freshness

MARKER = "_validated_media_edited_at"


async def capture(row):
    current = await fetch_one("SELECT s.content_edited_at, " + media_freshness.REVISION_SQL +
                              " AS media_edited_at FROM content_submissions s WHERE s.id = $1", row["id"])
    return current if current and current["content_edited_at"] == row.get("content_edited_at") else None


async def record(analysis_id, row, captured):
    if captured is None:
        return
    stamp = captured["media_edited_at"]
    # Only called after the full model-input hash matches. A concurrent post or
    # media edit prevents validation; the original input, result and times stay intact.
    await fetch_one("""UPDATE content_sentiment_analyses a SET input = jsonb_set(
        COALESCE(a.input, '{}'::jsonb), '{_validated_media_edited_at}', $2::jsonb, true)
        FROM content_submissions s WHERE a.id = $1 AND a.submission_id = s.id AND s.id = $3
          AND s.content_edited_at IS NOT DISTINCT FROM $5::timestamptz AND s.current_version = $6
          AND (""" + media_freshness.REVISION_SQL + """) IS NOT DISTINCT FROM $4::timestamptz RETURNING a.id""",
        analysis_id, json.dumps(stamp.isoformat() if stamp else None), row["id"], stamp,
        row.get("content_edited_at"), row["current_version"])


async def is_current(row):
    media = await fetch_one("SELECT " + media_freshness.REVISION_SQL +
                            " AS media_edited_at FROM content_submissions s WHERE s.id = $1", row["submission_id"])
    if not media:
        return False
    inp = json.loads(row["input"]) if isinstance(row.get("input"), str) else row.get("input") or {}
    check = {"checked_media_edited_at": inp[MARKER]} if MARKER in inp else {"checked_at": row["created_at"]}
    return media_freshness.is_current(media, check)


async def reuse(analysis_id, row, captured, placeholder_id=None):
    await record(analysis_id, row, captured)
    if placeholder_id:
        await fetch_one("DELETE FROM content_sentiment_analyses WHERE id = $1 RETURNING id", placeholder_id)
    return analysis_id
