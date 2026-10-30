"""Public mood: topics from the social listening source (Sprout Social, mocked for now), dated snapshots of how people
feel about each one, and saved per-post summaries. Snapshots are shared across posts, so one fetch serves everyone
until it goes stale."""

import logging

from postgresql_db.database import get_pool

logger = logging.getLogger(__name__)


async def setup() -> None:
    pool = get_pool()
    async with pool.acquire() as conn:
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS listening_topics (
                id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
                provider VARCHAR(30) NOT NULL,
                external_id VARCHAR(200) NOT NULL,
                name VARCHAR(200) NOT NULL,
                description TEXT,
                keywords TEXT[] NOT NULL DEFAULT '{}',
                is_mock BOOLEAN NOT NULL DEFAULT false,
                synced_at TIMESTAMP WITH TIME ZONE DEFAULT NOW() NOT NULL,
                deleted_at TIMESTAMP WITH TIME ZONE,
                UNIQUE (provider, external_id)
            );

            CREATE TABLE IF NOT EXISTS topic_snapshots (
                id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
                topic_id UUID NOT NULL REFERENCES listening_topics(id) ON DELETE CASCADE,
                window_days INTEGER NOT NULL,
                positive_pct NUMERIC(5, 1) NOT NULL,
                neutral_pct NUMERIC(5, 1) NOT NULL,
                negative_pct NUMERIC(5, 1) NOT NULL,
                volume INTEGER NOT NULL,
                volume_change_pct NUMERIC(8, 1),
                is_spike BOOLEAN NOT NULL DEFAULT false,
                daily_volume INTEGER[] NOT NULL DEFAULT '{}',
                top_posts JSONB NOT NULL DEFAULT '[]',
                is_mock BOOLEAN NOT NULL DEFAULT false,
                fetched_at TIMESTAMP WITH TIME ZONE DEFAULT NOW() NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_topic_snapshots_latest ON topic_snapshots(topic_id, fetched_at DESC);

            CREATE TABLE IF NOT EXISTS public_mood_summaries (
                id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
                submission_id UUID NOT NULL REFERENCES content_submissions(id) ON DELETE CASCADE,
                version_number INTEGER NOT NULL,
                snapshot_ids UUID[] NOT NULL DEFAULT '{}',
                -- The post's text plus the snapshots used; a different hash means the summary is out of date.
                input_hash VARCHAR(64) NOT NULL,
                result JSONB NOT NULL,
                model VARCHAR(100),
                is_mock BOOLEAN NOT NULL DEFAULT false,
                created_by UUID REFERENCES users(id) ON DELETE SET NULL,
                created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW() NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_public_mood_summaries_sub
                ON public_mood_summaries(submission_id, created_at DESC);

            -- The team's own tuning of a topic, kept across syncs from the source: extra words that mean it,
            -- source keywords too broad to count, and topics watched closely (refreshed as often as spiking ones).
            ALTER TABLE listening_topics ADD COLUMN IF NOT EXISTS extra_keywords TEXT[] NOT NULL DEFAULT '{}';
            ALTER TABLE listening_topics ADD COLUMN IF NOT EXISTS excluded_keywords TEXT[] NOT NULL DEFAULT '{}';
            ALTER TABLE listening_topics ADD COLUMN IF NOT EXISTS is_watched BOOLEAN NOT NULL DEFAULT false;
            ALTER TABLE listening_topics ADD COLUMN IF NOT EXISTS updated_by UUID REFERENCES users(id) ON DELETE SET NULL;
            ALTER TABLE listening_topics ADD COLUMN IF NOT EXISTS updated_at TIMESTAMP WITH TIME ZONE;

            -- "This topic isn't what the post is about": the topic is left out for that post, and the count per
            -- topic shows which keywords cause wrong matches.
            CREATE TABLE IF NOT EXISTS public_mood_topic_dismissals (
                submission_id UUID NOT NULL REFERENCES content_submissions(id) ON DELETE CASCADE,
                topic_id UUID NOT NULL REFERENCES listening_topics(id) ON DELETE CASCADE,
                matched_terms TEXT[] NOT NULL DEFAULT '{}',
                user_id UUID REFERENCES users(id) ON DELETE SET NULL,
                created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW() NOT NULL,
                PRIMARY KEY (submission_id, topic_id)
            );

            -- Whether a summary helped, one vote per person.
            CREATE TABLE IF NOT EXISTS public_mood_feedback (
                summary_id UUID NOT NULL REFERENCES public_mood_summaries(id) ON DELETE CASCADE,
                user_id UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                verdict VARCHAR(20) NOT NULL,
                note TEXT,
                created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW() NOT NULL,
                PRIMARY KEY (summary_id, user_id)
            );
        """)
    logger.info("Listening schema ready")
