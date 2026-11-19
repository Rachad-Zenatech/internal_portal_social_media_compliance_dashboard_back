"""A person's detected email time zone, independent of Graph-managed user fields."""
from uuid import UUID
import asyncpg
from postgresql_db.database import fetch_one
from postgresql_db.schemas.setup_review_email_schema import setup as setup_review_email_schema
from models.notifications.review_email_model import EmailTimezonePreference

_UPSERT_SQL = """
    INSERT INTO review_email_preferences (user_id, time_zone) VALUES ($1, $2)
    ON CONFLICT (user_id) DO UPDATE SET time_zone = EXCLUDED.time_zone, updated_at = NOW()
    RETURNING time_zone
"""


async def save_timezone(user_id: str, preference: EmailTimezonePreference) -> dict:
    try:
        row = await fetch_one(_UPSERT_SQL, UUID(user_id), preference.time_zone)
    except asyncpg.UndefinedTableError:
        await setup_review_email_schema()
        row = await fetch_one(_UPSERT_SQL, UUID(user_id), preference.time_zone)
    return dict(row)
