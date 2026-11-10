"""Microsoft Teams direct messages: where each person's chat with the portal's bot is, so notifications can reach them there.

A row is made when someone installs the bot or writes to it (Teams sends the bot a message with the chat's address). It
is linked to the portal user by their Microsoft Entra id, or failing that their email."""

import logging

from postgresql_db.database import get_pool

logger = logging.getLogger(__name__)


async def setup() -> None:
    pool = get_pool()
    async with pool.acquire() as conn:
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS teams_conversations (
                id SERIAL PRIMARY KEY,
                -- The portal user this chat belongs to. Null until the person is matched (by Entra id or email).
                user_id UUID REFERENCES users(id) ON DELETE CASCADE,
                -- The person's Microsoft Entra object id, as Teams reports it.
                aad_object_id VARCHAR(64) NOT NULL UNIQUE,
                -- Where the chat lives: the bot's messages to this person are posted to service_url / conversation_id.
                conversation_id VARCHAR(512) NOT NULL,
                service_url VARCHAR(512) NOT NULL,
                tenant_id VARCHAR(64),
                email VARCHAR(255),
                -- The person can pause Teams messages by writing "pause" to the bot, and resume with "resume".
                enabled BOOLEAN NOT NULL DEFAULT TRUE,
                -- False once Teams says the chat is gone (the bot was removed or the person left).
                active BOOLEAN NOT NULL DEFAULT TRUE,
                last_error TEXT,
                last_sent_at TIMESTAMP WITH TIME ZONE,
                created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW() NOT NULL,
                updated_at TIMESTAMP WITH TIME ZONE DEFAULT NOW() NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_teams_conversations_user ON teams_conversations(user_id);
        """)
    logger.info("Teams schema ready")
