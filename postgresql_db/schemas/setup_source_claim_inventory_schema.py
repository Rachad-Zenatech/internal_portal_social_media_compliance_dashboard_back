"""Durable claim identification shared by workers and repeated source checks."""
from postgresql_db.database import get_pool


async def setup():
    async with get_pool().acquire() as conn:
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS source_claim_inventories (
                content_key TEXT PRIMARY KEY,
                claims JSONB NOT NULL,
                created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
            )
        """)
