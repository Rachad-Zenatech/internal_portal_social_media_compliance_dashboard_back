"""Schema helper for submission source_links column."""

from typing import Any


async def ensure_source_links_column(conn: Any) -> None:
    await conn.execute(
        "ALTER TABLE content_submissions ADD COLUMN IF NOT EXISTS source_links TEXT[] NOT NULL DEFAULT '{}';"
    )

