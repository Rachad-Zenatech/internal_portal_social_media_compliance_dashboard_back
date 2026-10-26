"""One-time sign-in codes: after Microsoft sign-in the browser gets a short-lived code in the redirect (never the
session token), and swaps it, together with a value only the browser that started the sign-in has, for the token.
Stored in the database so any server worker can redeem it."""

import logging

from postgresql_db.database import get_pool

logger = logging.getLogger(__name__)


async def setup() -> None:
    pool = get_pool()
    async with pool.acquire() as conn:
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS login_codes (
                -- SHA-256 of the code and of the browser's nonce; the raw values are never stored.
                code_hash VARCHAR(64) PRIMARY KEY,
                nonce_hash VARCHAR(64) NOT NULL,
                user_id UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                expires_at TIMESTAMP WITH TIME ZONE NOT NULL,
                used_at TIMESTAMP WITH TIME ZONE,
                created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW() NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_login_codes_expires ON login_codes(expires_at);
        """)
    logger.info("Login codes schema ready")
