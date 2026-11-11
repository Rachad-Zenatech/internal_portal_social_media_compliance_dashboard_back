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

            -- Each scrape or import of the listening feed. Items are kept whatever a run returns, so a partial or
            -- empty run can't erase what was collected before.
            CREATE TABLE IF NOT EXISTS listening_collection_runs (
                id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
                provider VARCHAR(30) NOT NULL,
                -- SUCCESS | PARTIAL | EMPTY | AUTH_REQUIRED | RATE_LIMITED | FAILED
                status VARCHAR(20) NOT NULL DEFAULT 'SUCCESS',
                item_count INTEGER NOT NULL DEFAULT 0,
                note TEXT,
                started_at TIMESTAMP WITH TIME ZONE DEFAULT NOW() NOT NULL,
                finished_at TIMESTAMP WITH TIME ZONE
            );

            -- Every news article / X post collected, once. item_key is stable across scrapes (the link, or the
            -- text when there is none); content_hash says whether the text changed; story_key groups the same story
            -- reported by several outlets. is_eligible says whether it may be found by current searches; items that
            -- age out stay here (with their vector removed) so past assessments can still show their evidence.
            CREATE TABLE IF NOT EXISTS listening_items (
                id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
                item_key VARCHAR(64) NOT NULL UNIQUE,
                topic_external_id VARCHAR(200) NOT NULL,
                topic_name VARCHAR(200) NOT NULL,
                network VARCHAR(30),
                author VARCHAR(200),
                url TEXT,
                text TEXT NOT NULL,
                content_hash VARCHAR(64) NOT NULL,
                version INTEGER NOT NULL DEFAULT 1,
                story_key VARCHAR(64) NOT NULL,
                sentiment VARCHAR(10) NOT NULL,
                compound NUMERIC(5, 3) NOT NULL DEFAULT 0,
                crisis_flags TEXT[] NOT NULL DEFAULT '{}',
                published_at TIMESTAMP WITH TIME ZONE,
                first_seen_at TIMESTAMP WITH TIME ZONE DEFAULT NOW() NOT NULL,
                last_seen_at TIMESTAMP WITH TIME ZONE DEFAULT NOW() NOT NULL,
                last_run_id UUID REFERENCES listening_collection_runs(id) ON DELETE SET NULL,
                is_eligible BOOLEAN NOT NULL DEFAULT true,
                -- "<embedding model>:<content_hash>" of what is in the vector index, null when not indexed.
                indexed_hash VARCHAR(200)
            );
            CREATE INDEX IF NOT EXISTS idx_listening_items_eligible ON listening_items(is_eligible, published_at DESC);

            -- Which search found an item: 'market' (a trend topic) or a company watch category (company, hashtags,
            -- products, links, events, competitors), and the query text.
            ALTER TABLE listening_items ADD COLUMN IF NOT EXISTS category VARCHAR(20);
            ALTER TABLE listening_items ADD COLUMN IF NOT EXISTS collected_by TEXT;

            -- Every text an item has had, so an assessment can show exactly what it read.
            CREATE TABLE IF NOT EXISTS listening_item_versions (
                item_id UUID NOT NULL REFERENCES listening_items(id) ON DELETE CASCADE,
                version INTEGER NOT NULL,
                content_hash VARCHAR(64) NOT NULL,
                text TEXT NOT NULL,
                sentiment VARCHAR(10) NOT NULL,
                compound NUMERIC(5, 3) NOT NULL DEFAULT 0,
                seen_at TIMESTAMP WITH TIME ZONE DEFAULT NOW() NOT NULL,
                PRIMARY KEY (item_id, version)
            );

            -- Small shared facts: evidence_version (changes when searchable items change), indexing_status.
            CREATE TABLE IF NOT EXISTS listening_state (
                key VARCHAR(60) PRIMARY KEY,
                value TEXT NOT NULL,
                updated_at TIMESTAMP WITH TIME ZONE DEFAULT NOW() NOT NULL
            );

            -- A saved reading of how a draft sits among the conversations found for it. cache_key covers the full
            -- draft context, the evidence version, the matching policy and the embedding model: a new one means a
            -- new assessment, and the old one stays as the record of what was shown.
            CREATE TABLE IF NOT EXISTS public_mood_assessments (
                id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
                submission_id UUID NOT NULL REFERENCES content_submissions(id) ON DELETE CASCADE,
                version_number INTEGER NOT NULL,
                cache_key VARCHAR(64) NOT NULL,
                context_hash VARCHAR(64) NOT NULL,
                evidence_version VARCHAR(64) NOT NULL,
                policy_version VARCHAR(60) NOT NULL,
                embedding_version VARCHAR(100) NOT NULL,
                -- READY | NO_RELEVANT_MATCHES
                status VARCHAR(30) NOT NULL DEFAULT 'READY',
                result JSONB NOT NULL,
                assessed_at TIMESTAMP WITH TIME ZONE DEFAULT NOW() NOT NULL,
                expires_at TIMESTAMP WITH TIME ZONE NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_public_mood_assessments_sub
                ON public_mood_assessments(submission_id, assessed_at DESC);

            -- The items an assessment looked at, with the exact version of each and the verdict on it.
            CREATE TABLE IF NOT EXISTS public_mood_assessment_items (
                assessment_id UUID NOT NULL REFERENCES public_mood_assessments(id) ON DELETE CASCADE,
                item_id UUID NOT NULL REFERENCES listening_items(id),
                item_version INTEGER NOT NULL,
                content_hash VARCHAR(64) NOT NULL,
                similarity_score NUMERIC(7, 4) NOT NULL,
                margin NUMERIC(7, 4),
                relevance_status VARCHAR(12) NOT NULL,
                relevance_reason TEXT,
                supporting_text TEXT,
                story_key VARCHAR(64) NOT NULL,
                -- Counted in the mood (the first of its story that is relevant) and/or shown as an example.
                counted BOOLEAN NOT NULL DEFAULT false,
                displayed BOOLEAN NOT NULL DEFAULT false,
                PRIMARY KEY (assessment_id, item_id)
            );

            -- What reviewers said about an assessment: that a retrieved item isn't about the post (the item is then
            -- left out of this draft's later assessments), or that its recommendation was wrong. Kept against the
            -- exact assessment they were looking at, so it can later be learned from.
            CREATE TABLE IF NOT EXISTS public_mood_assessment_feedback (
                id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
                assessment_id UUID NOT NULL REFERENCES public_mood_assessments(id) ON DELETE CASCADE,
                -- MATCH_UNRELATED | RECOMMENDATION_CORRECTION
                kind VARCHAR(30) NOT NULL,
                item_id UUID REFERENCES listening_items(id),
                recommendation VARCHAR(10),
                reason_code VARCHAR(40) NOT NULL,
                note TEXT,
                user_id UUID REFERENCES users(id) ON DELETE SET NULL,
                created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW() NOT NULL
            );
            CREATE UNIQUE INDEX IF NOT EXISTS uq_pm_feedback_item
                ON public_mood_assessment_feedback(assessment_id, user_id, item_id) WHERE kind = 'MATCH_UNRELATED';
            CREATE UNIQUE INDEX IF NOT EXISTS uq_pm_feedback_correction
                ON public_mood_assessment_feedback(assessment_id, user_id) WHERE kind = 'RECOMMENDATION_CORRECTION';
        """)
    logger.info("Listening schema ready")
