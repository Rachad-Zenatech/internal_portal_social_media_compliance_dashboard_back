"""A person's detected email time zone, independent of Graph-managed user fields."""
from uuid import UUID
from postgresql_db.database import fetch_one
from models.notifications.review_email_model import EmailTimezonePreference


async def save_timezone(user_id: str, preference: EmailTimezonePreference) -> dict:
    row = await fetch_one("""
        INSERT INTO review_email_preferences (user_id, time_zone) VALUES ($1, $2)
        ON CONFLICT (user_id) DO UPDATE SET time_zone = EXCLUDED.time_zone, updated_at = NOW()
        RETURNING time_zone
    """, UUID(user_id), preference.time_zone)
    return dict(row)
