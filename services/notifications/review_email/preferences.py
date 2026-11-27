"""A person's portal and email time zone, independent of Graph-managed fields."""
from uuid import UUID
import asyncpg
from postgresql_db.database import fetch_one
from postgresql_db.schemas.setup_review_email_schema import setup as setup_review_email_schema
from models.notifications.review_email_model import EmailTimezonePreference

_UPSERT_SQL = """
    INSERT INTO review_email_preferences (user_id, time_zone, source) VALUES ($1, $2, $3)
    ON CONFLICT (user_id) DO UPDATE SET
        time_zone = CASE WHEN $3 = 'MANUAL' OR review_email_preferences.source = 'BROWSER'
            THEN EXCLUDED.time_zone ELSE review_email_preferences.time_zone END,
        source = CASE WHEN $3 = 'MANUAL' THEN 'MANUAL' ELSE review_email_preferences.source END,
        updated_at = CASE WHEN $3 = 'MANUAL' OR review_email_preferences.source = 'BROWSER'
            THEN NOW() ELSE review_email_preferences.updated_at END
    RETURNING time_zone, source
"""


async def save_timezone(user_id: str, preference: EmailTimezonePreference) -> dict:
    try:
        row = await fetch_one(_UPSERT_SQL, UUID(user_id), preference.time_zone, preference.source)
    except (asyncpg.UndefinedTableError, asyncpg.UndefinedColumnError):
        await setup_review_email_schema()
        row = await fetch_one(_UPSERT_SQL, UUID(user_id), preference.time_zone, preference.source)
    return dict(row)


async def get_timezone(user_id: str) -> dict:
    query = "SELECT time_zone, source FROM review_email_preferences WHERE user_id = $1"
    try:
        row = await fetch_one(query, UUID(user_id))
    except (asyncpg.UndefinedTableError, asyncpg.UndefinedColumnError):
        await setup_review_email_schema()
        row = await fetch_one(query, UUID(user_id))
    return dict(row) if row else {"time_zone": None, "source": "BROWSER"}
