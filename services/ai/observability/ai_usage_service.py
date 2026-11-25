"""Record AI usage without imposing application-level usage caps."""

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Dict, Literal, Optional
from uuid import UUID

from fastapi import HTTPException

from postgresql_db.database import get_pool

Feature = Literal["SENTIMENT", "TRANSCRIPTION", "PUBLIC_MOOD", "CAPTION", "CHAT", "SOURCE_CHECK", "IMAGE_TEXT", "SCRIPT_FIX"]
FEATURE_NAMES: Dict[str, str] = {
    "SENTIMENT": "sentiment analyses",
    "TRANSCRIPTION": "video transcriptions",
    "PUBLIC_MOOD": "public mood summaries",
    "CAPTION": "caption suggestions",
    "CHAT": "assistant chat replies",
    "SOURCE_CHECK": "source link checks",
    "IMAGE_TEXT": "image reads",
    "SCRIPT_FIX": "script fix suggestions",
}

DAY = timedelta(hours=24)


def limit(feature: Feature, scope: str) -> int:
    return 0


def per_user_minute() -> int:
    return 0


@dataclass
class Usage:
    used: int
    limit: int
    resets_at: Optional[datetime]  # when the oldest call in the window drops out


class AiLimitExceeded(HTTPException):
    def __init__(self, message: str):
        super().__init__(status_code=429, detail=message)


async def _count(conn, where: str, args: list, window: timedelta) -> Usage:
    row = await conn.fetchrow(
        f"""
        SELECT count(*) AS used, min(created_at) AS oldest
        FROM ai_usage_events
        WHERE created_at > NOW() - $1::interval AND {where}
        """,
        window,
        *args,
    )
    return Usage(int(row["used"]), 0, row["oldest"] + window if row["oldest"] else None)


async def reserve(feature: Feature, user_id: Optional[UUID], submission_id: Optional[UUID] = None) -> None:
    """Record an AI request for usage reporting without enforcing a cap."""
    pool = get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO ai_usage_events (feature, user_id, submission_id) VALUES ($1, $2, $3)",
            feature, user_id, submission_id,
        )


async def usage_for(user_id: UUID, submission_id: Optional[UUID] = None) -> Dict[str, dict]:
    """What a person has left, per feature (and for one submission when given), for showing in the UI."""
    out: Dict[str, dict] = {}
    pool = get_pool()
    async with pool.acquire() as conn:
        for feature in FEATURE_NAMES:
            mine = await _count(conn, "feature = $2 AND user_id = $3", [feature, user_id], DAY)
            everyone = await _count(conn, "feature = $2", [feature], DAY)
            user_cap, global_cap = limit(feature, "PER_USER_DAY"), limit(feature, "GLOBAL_DAY")
            remaining = [c - u for c, u in ((user_cap, mine.used), (global_cap, everyone.used)) if c]
            entry = {
                "used": mine.used,
                "limit": user_cap,
                "company_used": everyone.used,
                "company_limit": global_cap,
                "resets_at": mine.resets_at,
                "company_resets_at": everyone.resets_at,
            }
            sub_cap = limit(feature, "PER_SUBMISSION_DAY")
            if submission_id and sub_cap:
                this = await _count(conn, "feature = $2 AND submission_id = $3", [feature, submission_id], DAY)
                entry["submission_used"], entry["submission_limit"] = this.used, sub_cap
                remaining.append(sub_cap - this.used)
            entry["remaining"] = max(0, min(remaining)) if remaining else None
            out[feature] = entry
    return out
