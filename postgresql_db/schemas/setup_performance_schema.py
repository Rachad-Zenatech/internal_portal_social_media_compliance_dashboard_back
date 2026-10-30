"""Post performance: dated snapshots of each published post's numbers on each platform (views, likes, comments,
shares, saves, comment sentiment). Kept over time so a drop in sentiment between checks can be spotted."""

import logging

from postgresql_db.database import get_pool

logger = logging.getLogger(__name__)


async def setup() -> None:
    pool = get_pool()
    async with pool.acquire() as conn:
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS post_metrics_snapshots (
                id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
                -- The submission's id, or "sample:N" for example posts shown while there's no real data.
                post_key VARCHAR(100) NOT NULL,
                submission_id UUID REFERENCES content_submissions(id) ON DELETE CASCADE,
                platform VARCHAR(30) NOT NULL,
                views INTEGER NOT NULL DEFAULT 0,
                likes INTEGER NOT NULL DEFAULT 0,
                comments INTEGER NOT NULL DEFAULT 0,
                shares INTEGER NOT NULL DEFAULT 0,
                saves INTEGER NOT NULL DEFAULT 0,
                daily_views INTEGER[] NOT NULL DEFAULT '{}',
                comment_positive_pct NUMERIC(5, 1),
                comment_neutral_pct NUMERIC(5, 1),
                comment_negative_pct NUMERIC(5, 1),
                comment_samples JSONB NOT NULL DEFAULT '[]',
                is_mock BOOLEAN NOT NULL DEFAULT false,
                fetched_at TIMESTAMP WITH TIME ZONE DEFAULT NOW() NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_post_metrics_latest ON post_metrics_snapshots(post_key, platform, fetched_at DESC);

            -- Which platform API the numbers came from, and every number it returned (platforms differ: impressions
            -- vs views, reactions, bookmarks, dislikes...). Numbers a platform doesn't provide are simply absent.
            ALTER TABLE post_metrics_snapshots ADD COLUMN IF NOT EXISTS api VARCHAR(30);
            ALTER TABLE post_metrics_snapshots ADD COLUMN IF NOT EXISTS metrics JSONB NOT NULL DEFAULT '{}';
        """)
    logger.info("Post performance schema ready")
