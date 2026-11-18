"""Durable delivery and activation boundary for review workflow emails."""
from postgresql_db.database import get_pool


async def setup() -> None:
    async with get_pool().acquire() as conn:
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS review_email_preferences (
                user_id UUID PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
                time_zone TEXT NOT NULL,
                updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
            );
            CREATE TABLE IF NOT EXISTS review_email_state (
                id INTEGER PRIMARY KEY CHECK (id = 1),
                activated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
            );
            INSERT INTO review_email_state (id) VALUES (1) ON CONFLICT DO NOTHING;
            CREATE TABLE IF NOT EXISTS review_email_outbox (
                id BIGSERIAL PRIMARY KEY,
                dedupe_key TEXT NOT NULL UNIQUE,
                user_id UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                kind TEXT NOT NULL,
                payload JSONB NOT NULL,
                status TEXT NOT NULL DEFAULT 'PENDING',
                attempts INTEGER NOT NULL DEFAULT 0,
                available_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                locked_until TIMESTAMPTZ,
                sent_at TIMESTAMPTZ,
                created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
            );
            CREATE INDEX IF NOT EXISTS idx_review_email_pending
                ON review_email_outbox (available_at, id) WHERE status = 'PENDING';
        """)
