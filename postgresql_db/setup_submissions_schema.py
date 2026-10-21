"""
Setup Content Submission Schema
Creates content_submissions with their files, versions, review jobs and activity events,
and (in development only) seeds clearly marked demo social accounts so the
"connected accounts only" picker has something to show before real OAuth exists.
"""
import asyncio
import logging
import os

from postgresql_db.database import get_pool, close_pool

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


async def setup():
    pool = get_pool()
    async with pool.acquire() as conn:
        logger.info("Setting up content submission schema...")

        await conn.execute("""
            CREATE SEQUENCE IF NOT EXISTS content_submission_ref_seq START 1001;

            CREATE TABLE IF NOT EXISTS content_submissions (
                id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
                reference_code VARCHAR(32) UNIQUE NOT NULL
                    DEFAULT ('SUB-' || nextval('content_submission_ref_seq')::text),
                title VARCHAR(300) NOT NULL,
                content_type VARCHAR(30) NOT NULL,
                brand_code VARCHAR(50),
                audience VARCHAR(50),
                region VARCHAR(100),
                platforms TEXT[] DEFAULT '{}'::text[] NOT NULL,
                account_id UUID REFERENCES oauth_connections(id) ON DELETE SET NULL,
                publish_at TIMESTAMP WITH TIME ZONE,
                caption_html TEXT,
                caption_text TEXT,
                rule_sets TEXT[] DEFAULT '{}'::text[] NOT NULL,
                reviewer_route TEXT[] DEFAULT '{}'::text[] NOT NULL,
                status VARCHAR(30) DEFAULT 'DRAFT' NOT NULL,
                current_version INT DEFAULT 0 NOT NULL,
                latest_check JSONB,
                submitted_at TIMESTAMP WITH TIME ZONE,
                created_by UUID REFERENCES users(id) ON DELETE SET NULL,
                updated_by UUID REFERENCES users(id) ON DELETE SET NULL,
                created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW() NOT NULL,
                updated_at TIMESTAMP WITH TIME ZONE DEFAULT NOW() NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_content_submissions_creator ON content_submissions(created_by, updated_at DESC);
            CREATE INDEX IF NOT EXISTS idx_content_submissions_status ON content_submissions(status);

            CREATE TABLE IF NOT EXISTS content_submission_files (
                id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
                submission_id UUID NOT NULL REFERENCES content_submissions(id) ON DELETE CASCADE,
                kind VARCHAR(20) NOT NULL,
                original_name VARCHAR(255) NOT NULL,
                mime_type VARCHAR(150) NOT NULL,
                size_bytes BIGINT NOT NULL,
                storage_path TEXT NOT NULL,
                uploaded_by UUID REFERENCES users(id) ON DELETE SET NULL,
                created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW() NOT NULL,
                deleted_at TIMESTAMP WITH TIME ZONE
            );
            CREATE INDEX IF NOT EXISTS idx_content_submission_files_submission ON content_submission_files(submission_id) WHERE deleted_at IS NULL;
            -- Drafts withdrawn after a review are hidden rather than erased, so their review history survives.
            ALTER TABLE content_submissions ADD COLUMN IF NOT EXISTS deleted_at TIMESTAMP WITH TIME ZONE;
            ALTER TABLE content_submissions ADD COLUMN IF NOT EXISTS deleted_by UUID REFERENCES users(id) ON DELETE SET NULL;

            -- Auto-generated speech transcript for video files (see services/transcription_service.py)
            ALTER TABLE content_submission_files ADD COLUMN IF NOT EXISTS transcript JSONB;
            ALTER TABLE content_submission_files ADD COLUMN IF NOT EXISTS transcript_status VARCHAR(20);
            ALTER TABLE content_submission_files ADD COLUMN IF NOT EXISTS transcript_error TEXT;
            ALTER TABLE content_submission_files ADD COLUMN IF NOT EXISTS transcript_updated_at TIMESTAMP WITH TIME ZONE;

            CREATE TABLE IF NOT EXISTS content_submission_versions (
                id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
                submission_id UUID NOT NULL REFERENCES content_submissions(id) ON DELETE CASCADE,
                version_number INT NOT NULL,
                snapshot JSONB NOT NULL,
                created_by UUID REFERENCES users(id) ON DELETE SET NULL,
                created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW() NOT NULL,
                CONSTRAINT content_submission_versions_unique UNIQUE (submission_id, version_number)
            );

            CREATE TABLE IF NOT EXISTS content_review_jobs (
                id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
                submission_id UUID NOT NULL REFERENCES content_submissions(id) ON DELETE CASCADE,
                version_number INT NOT NULL,
                status VARCHAR(30) DEFAULT 'PENDING' NOT NULL,
                reviewer_route TEXT[] DEFAULT '{}'::text[] NOT NULL,
                automated_check JSONB,
                traffic_light VARCHAR(10),
                score INT,
                created_by UUID REFERENCES users(id) ON DELETE SET NULL,
                created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW() NOT NULL,
                completed_at TIMESTAMP WITH TIME ZONE
            );
            CREATE INDEX IF NOT EXISTS idx_content_review_jobs_submission ON content_review_jobs(submission_id, created_at DESC);

            CREATE TABLE IF NOT EXISTS content_submission_events (
                id BIGSERIAL PRIMARY KEY,
                submission_id UUID NOT NULL REFERENCES content_submissions(id) ON DELETE CASCADE,
                event_type VARCHAR(40) NOT NULL,
                message TEXT NOT NULL,
                data JSONB,
                actor_user_id UUID REFERENCES users(id) ON DELETE SET NULL,
                created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW() NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_content_submission_events_submission ON content_submission_events(submission_id, created_at DESC);

            -- Gemini sentiment analysis (see services/sentiment_service.py). Advisory only.
            -- input is exactly what was analysed; content_hash lets an unchanged draft reuse its result.
            CREATE TABLE IF NOT EXISTS content_sentiment_analyses (
                id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
                submission_id UUID NOT NULL REFERENCES content_submissions(id) ON DELETE CASCADE,
                version_number INT NOT NULL DEFAULT 0,
                trigger VARCHAR(20) NOT NULL,
                status VARCHAR(20) NOT NULL,
                content_hash CHAR(64),
                input JSONB,
                result JSONB,
                model VARCHAR(100),
                prompt_version VARCHAR(40) NOT NULL,
                error TEXT,
                attempts INT NOT NULL DEFAULT 0,
                requested_by UUID REFERENCES users(id) ON DELETE SET NULL,
                created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW() NOT NULL,
                updated_at TIMESTAMP WITH TIME ZONE DEFAULT NOW() NOT NULL,
                completed_at TIMESTAMP WITH TIME ZONE
            );
            CREATE INDEX IF NOT EXISTS idx_content_sentiment_submission ON content_sentiment_analyses(submission_id, created_at DESC);
            CREATE INDEX IF NOT EXISTS idx_content_sentiment_status ON content_sentiment_analyses(status)
                WHERE status IN ('WAITING_TRANSCRIPT', 'PENDING', 'PROCESSING');

            -- One row per Gemini call actually made, for usage limits (see services/ai_usage_service.py).
            CREATE TABLE IF NOT EXISTS ai_usage_events (
                id BIGSERIAL PRIMARY KEY,
                feature VARCHAR(30) NOT NULL,
                user_id UUID REFERENCES users(id) ON DELETE SET NULL,
                submission_id UUID REFERENCES content_submissions(id) ON DELETE SET NULL,
                created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW() NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_ai_usage_feature_time ON ai_usage_events(feature, created_at DESC);
            CREATE INDEX IF NOT EXISTS idx_ai_usage_user_time ON ai_usage_events(user_id, created_at DESC);
            CREATE INDEX IF NOT EXISTS idx_ai_usage_submission_time ON ai_usage_events(submission_id, created_at DESC);
        """)

        # Brands named in the brand guide (ZooOffice groups DeskFlex, Othership and others).
        await conn.execute("""
            INSERT INTO brands (code, name, sort_order)
            VALUES
                ('DAAS', 'DaaS (Drone as a Service)', 45),
                ('ZOOOFFICE', 'ZooOffice', 50),
                ('ZENASTUDIOS', 'Zena Studios', 60)
            ON CONFLICT (code) DO NOTHING;
        """)

        if os.getenv("APP_ENV", "development").lower() != "production":
            await _seed_demo_social_accounts(conn)

        logger.info("Content submission schema setup successfully completed!")


async def _seed_demo_social_accounts(conn):
    """Insert demo organic social accounts once, flagged demo_seed in their metadata."""
    already = await conn.fetchval(
        "SELECT EXISTS (SELECT 1 FROM oauth_connections WHERE connection_metadata->>'demo_seed' = 'true')"
    )
    if already:
        return

    org_id = await conn.fetchval("SELECT id FROM organizations ORDER BY created_at ASC LIMIT 1")
    if not org_id:
        return

    accounts = [
        # provider, account name, handle, brand code, company code, is_ceo
        ("LINKEDIN_PAGE", "ZenaTech", "zenatech", "ZENATECH", "ZTG", False),
        ("X_TWITTER", "ZenaTech", "@ZenaTechInc", "ZENATECH", "ZTG", False),
        ("YOUTUBE", "ZenaTech", "@ZenaTech", "ZENATECH", "ZTG", False),
        ("TIKTOK", "ZenaTech", "@zenatech", "ZENATECH", "ZTG", False),
        ("LINKEDIN_PAGE", "ZenaDrone", "zenadrone", "ZENADRONE", "ZDR", False),
        ("YOUTUBE", "ZenaDrone", "@ZenaDrone", "ZENADRONE", "ZDR", False),
        ("INSTAGRAM", "ZenaDrone", "@zenadrone", "ZENADRONE", "ZDR", False),
        ("FACEBOOK_PAGE", "ZenaDrone", "ZenaDroneOfficial", "ZENADRONE", "ZDR", False),
        ("LINKEDIN_PROFILE", "Shaun – CEO", "shaun-ceo", "ZENATECH", "ZTG", True),
        ("X_TWITTER", "Shaun – CEO", "@ShaunCEO", "ZENATECH", "ZTG", True),
    ]
    await conn.execute(
        """
        INSERT INTO oauth_connections (
            organization_id, company_id, provider, provider_account_id, account_name,
            access_token_encrypted, status, expires_at, connection_metadata
        )
        SELECT
            $1,
            (SELECT id FROM companies WHERE organization_id = $1 AND code = a.company_code LIMIT 1),
            a.provider,
            'demo-' || lower(a.provider) || '-' || a.handle,
            a.account_name,
            'demo-token-not-a-real-credential',
            'CONNECTED',
            NOW() + INTERVAL '365 days',
            jsonb_build_object('demo_seed', true, 'handle', a.handle, 'brand_code', a.brand_code, 'is_ceo', a.is_ceo)
        FROM unnest($2::text[], $3::text[], $4::text[], $5::text[], $6::text[], $7::bool[])
            AS a(provider, account_name, handle, brand_code, company_code, is_ceo)
        """,
        org_id,
        [a[0] for a in accounts],
        [a[1] for a in accounts],
        [a[2] for a in accounts],
        [a[3] for a in accounts],
        [a[4] for a in accounts],
        [a[5] for a in accounts],
    )
    logger.info("Seeded %d demo social accounts for development.", len(accounts))


async def _main():
    from postgresql_db.database import create_pool
    await create_pool()
    try:
        await setup()
    finally:
        await close_pool()


if __name__ == "__main__":
    asyncio.run(_main())
