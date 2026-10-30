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
            -- Deadlines: when the reviewer (REVIEW) or the creator (REVISION) needs to act, and which notices went out
            ALTER TABLE content_submissions ADD COLUMN IF NOT EXISTS due_at TIMESTAMP WITH TIME ZONE;
            ALTER TABLE content_submissions ADD COLUMN IF NOT EXISTS due_kind VARCHAR(20);
            ALTER TABLE content_submissions ADD COLUMN IF NOT EXISTS due_reminded_at TIMESTAMP WITH TIME ZONE;
            ALTER TABLE content_submissions ADD COLUMN IF NOT EXISTS due_overdue_notified_at TIMESTAMP WITH TIME ZONE;
            -- When the creator needs the post approved by; it becomes the reviewer's deadline on submit.
            ALTER TABLE content_submissions ADD COLUMN IF NOT EXISTS approval_needed_by TIMESTAMP WITH TIME ZONE;
            CREATE INDEX IF NOT EXISTS idx_content_submissions_due ON content_submissions(due_at)
                WHERE due_at IS NOT NULL AND deleted_at IS NULL;
            ALTER TABLE content_submissions ADD COLUMN IF NOT EXISTS deleted_by UUID REFERENCES users(id) ON DELETE SET NULL;

            -- Brands each user works on. A user with none listed (and every super admin) works on all brands.
            CREATE TABLE IF NOT EXISTS user_brands (
                user_id UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                brand_code VARCHAR(50) NOT NULL REFERENCES brands(code) ON DELETE CASCADE,
                assigned_at TIMESTAMP WITH TIME ZONE DEFAULT NOW() NOT NULL,
                assigned_by UUID REFERENCES users(id) ON DELETE SET NULL,
                PRIMARY KEY (user_id, brand_code)
            );
            CREATE INDEX IF NOT EXISTS idx_content_submissions_brand ON content_submissions(brand_code) WHERE deleted_at IS NULL;

            -- Approval paths: who gives final approval for which brand / audience / Shaun's own posts.
            -- Empty brand or audience means any. Content no path matches can be approved by any brand reviewer.
            CREATE TABLE IF NOT EXISTS approval_paths (
                id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
                brand_code VARCHAR(50) REFERENCES brands(code) ON DELETE CASCADE,
                audience VARCHAR(50),
                ceo_content BOOLEAN NOT NULL DEFAULT false,
                label VARCHAR(200),
                approver_ids UUID[] NOT NULL DEFAULT '{}',
                created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW() NOT NULL,
                updated_at TIMESTAMP WITH TIME ZONE DEFAULT NOW() NOT NULL,
                created_by UUID REFERENCES users(id) ON DELETE SET NULL,
                updated_by UUID REFERENCES users(id) ON DELETE SET NULL
            );

            -- Canva: each user's connected account (mock until the Connect API keys exist), the mock designs'
            -- revisions, and which design and revision a file was imported from (for "reimport latest").
            CREATE TABLE IF NOT EXISTS canva_connections (
                user_id UUID PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
                mode VARCHAR(10) NOT NULL DEFAULT 'MOCK',
                display_name VARCHAR(255),
                connected_at TIMESTAMP WITH TIME ZONE DEFAULT NOW() NOT NULL
            );
            CREATE TABLE IF NOT EXISTS canva_mock_revisions (
                user_id UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                design_id VARCHAR(100) NOT NULL,
                revision INT NOT NULL DEFAULT 1,
                updated_at TIMESTAMP WITH TIME ZONE DEFAULT NOW() NOT NULL,
                PRIMARY KEY (user_id, design_id)
            );
            ALTER TABLE content_submission_files ADD COLUMN IF NOT EXISTS source VARCHAR(20) NOT NULL DEFAULT 'UPLOAD';
            ALTER TABLE content_submission_files ADD COLUMN IF NOT EXISTS canva_design_id VARCHAR(200);
            ALTER TABLE content_submission_files ADD COLUMN IF NOT EXISTS canva_design_title VARCHAR(300);
            ALTER TABLE content_submission_files ADD COLUMN IF NOT EXISTS canva_revision INT;

            -- A post can go out through several connected accounts. account_id stays as the first (primary) one,
            -- which the calendar, screener and publish page use.
            ALTER TABLE content_submissions ADD COLUMN IF NOT EXISTS account_ids UUID[] NOT NULL DEFAULT '{}';
            UPDATE content_submissions SET account_ids = ARRAY[account_id]
            WHERE account_id IS NOT NULL AND cardinality(account_ids) = 0;

            -- Auto-generated speech transcript for video files (see services/compliance/transcription_service.py)
            ALTER TABLE content_submission_files ADD COLUMN IF NOT EXISTS transcript JSONB;
            ALTER TABLE content_submission_files ADD COLUMN IF NOT EXISTS transcript_status VARCHAR(20);
            ALTER TABLE content_submission_files ADD COLUMN IF NOT EXISTS transcript_error TEXT;
            ALTER TABLE content_submission_files ADD COLUMN IF NOT EXISTS transcript_updated_at TIMESTAMP WITH TIME ZONE;
            -- Text shown in an image (a headline on a graphic, figures, fine print), read once so the rulebook
            -- check can screen it like the caption (see services/compliance/image_text_service.py).
            ALTER TABLE content_submission_files ADD COLUMN IF NOT EXISTS image_text TEXT;
            ALTER TABLE content_submission_files ADD COLUMN IF NOT EXISTS image_text_status VARCHAR(20);
            ALTER TABLE content_submission_files ADD COLUMN IF NOT EXISTS image_text_error TEXT;
            ALTER TABLE content_submission_files ADD COLUMN IF NOT EXISTS image_text_updated_at TIMESTAMP WITH TIME ZONE;
            -- Text of a script (or a blog's article file), read once so the rulebook check screens it like the
            -- caption (see services/compliance/document_text_service.py).
            ALTER TABLE content_submission_files ADD COLUMN IF NOT EXISTS doc_text TEXT;
            ALTER TABLE content_submission_files ADD COLUMN IF NOT EXISTS doc_text_status VARCHAR(20);
            ALTER TABLE content_submission_files ADD COLUMN IF NOT EXISTS doc_text_error TEXT;
            ALTER TABLE content_submission_files ADD COLUMN IF NOT EXISTS doc_text_updated_at TIMESTAMP WITH TIME ZONE;
            -- When each reading job last started, so screens can say how long the check has left: *_updated_at
            -- moves again when the job finishes, and finished jobs' durations give the estimate
            -- (see services/analysis_progress_service.py).
            ALTER TABLE content_submission_files ADD COLUMN IF NOT EXISTS transcript_started_at TIMESTAMP WITH TIME ZONE;
            ALTER TABLE content_submission_files ADD COLUMN IF NOT EXISTS image_text_started_at TIMESTAMP WITH TIME ZONE;
            ALTER TABLE content_submission_files ADD COLUMN IF NOT EXISTS doc_text_started_at TIMESTAMP WITH TIME ZONE;

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

            -- Reviewer decision on each review job (one job per submitted version).
            ALTER TABLE content_review_jobs ADD COLUMN IF NOT EXISTS decision VARCHAR(30);
            ALTER TABLE content_review_jobs ADD COLUMN IF NOT EXISTS decision_notes TEXT;
            ALTER TABLE content_review_jobs ADD COLUMN IF NOT EXISTS decided_by UUID REFERENCES users(id) ON DELETE SET NULL;
            ALTER TABLE content_review_jobs ADD COLUMN IF NOT EXISTS decided_at TIMESTAMP WITH TIME ZONE;
            ALTER TABLE content_review_jobs ALTER COLUMN status SET DEFAULT 'AWAITING_REVIEW';
            -- Rules the automated check can't test from text, which the approving reviewer confirmed by hand.
            ALTER TABLE content_review_jobs ADD COLUMN IF NOT EXISTS manual_checks JSONB;

            -- Reviewers' calls on automated compliance flags, per version: a dismissal with its reason.
            CREATE TABLE IF NOT EXISTS flag_reviews (
                id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
                submission_id UUID NOT NULL REFERENCES content_submissions(id) ON DELETE CASCADE,
                version_number INT NOT NULL,
                rule_id UUID NOT NULL,
                rule_number VARCHAR(50) NOT NULL,
                flagged_terms TEXT[] NOT NULL DEFAULT '{}',
                traffic_light VARCHAR(20) NOT NULL,
                reason VARCHAR(40) NOT NULL,
                note TEXT,
                decided_by UUID REFERENCES users(id) ON DELETE SET NULL,
                decided_at TIMESTAMP WITH TIME ZONE DEFAULT NOW() NOT NULL,
                UNIQUE (submission_id, version_number, rule_id)
            );
            CREATE INDEX IF NOT EXISTS idx_flag_reviews_rule ON flag_reviews(rule_number);

            -- Review comments: what the reviewer wants changed (optionally pinned to a moment in a video), and the
            -- creator's replies. Tied to the version they were written on, so the history reads correctly.
            CREATE TABLE IF NOT EXISTS content_review_comments (
                id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
                submission_id UUID NOT NULL REFERENCES content_submissions(id) ON DELETE CASCADE,
                version_number INT NOT NULL DEFAULT 0,
                author_id UUID REFERENCES users(id) ON DELETE SET NULL,
                category VARCHAR(20) NOT NULL DEFAULT 'NOTE',
                body TEXT NOT NULL,
                start_sec NUMERIC(10, 2),
                end_sec NUMERIC(10, 2),
                resolved_at TIMESTAMP WITH TIME ZONE,
                resolved_by UUID REFERENCES users(id) ON DELETE SET NULL,
                created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW() NOT NULL,
                updated_at TIMESTAMP WITH TIME ZONE DEFAULT NOW() NOT NULL,
                deleted_at TIMESTAMP WITH TIME ZONE
            );
            CREATE INDEX IF NOT EXISTS idx_content_review_comments_submission
                ON content_review_comments(submission_id, created_at) WHERE deleted_at IS NULL;
            -- Whether the author wrote it as the reviewer (a super admin can review their own upload)
            ALTER TABLE content_review_comments ADD COLUMN IF NOT EXISTS from_reviewer BOOLEAN;

            -- Gemini sentiment analysis (see services/compliance/sentiment_service.py). Advisory only.
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
            -- When Gemini's daily limit resets (FAILED on quota), or when a SCHEDULED run is due.
            ALTER TABLE content_sentiment_analyses ADD COLUMN IF NOT EXISTS retry_at TIMESTAMP WITH TIME ZONE;
            CREATE INDEX IF NOT EXISTS idx_content_sentiment_scheduled ON content_sentiment_analyses(retry_at)
                WHERE status = 'SCHEDULED';

            -- Reviewers' verdict on each finding ("useful" / "not an issue"). category and basis are copied in so
            -- the noise report can group by kind of finding without reading every result.
            CREATE TABLE IF NOT EXISTS content_sentiment_feedback (
                analysis_id UUID NOT NULL REFERENCES content_sentiment_analyses(id) ON DELETE CASCADE,
                finding_id VARCHAR(32) NOT NULL,
                user_id UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                verdict VARCHAR(20) NOT NULL,
                note TEXT,
                category VARCHAR(30),
                basis VARCHAR(30),
                quote TEXT,
                created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW() NOT NULL,
                updated_at TIMESTAMP WITH TIME ZONE DEFAULT NOW() NOT NULL,
                PRIMARY KEY (analysis_id, finding_id, user_id)
            );

            -- Test set: an analysis input frozen as it was, with the verdict a reviewer agrees is right. The eval
            -- script (scripts/sentiment_eval.py) re-runs these after prompt or model changes.
            CREATE TABLE IF NOT EXISTS sentiment_eval_cases (
                id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
                analysis_id UUID REFERENCES content_sentiment_analyses(id) ON DELETE SET NULL,
                submission_id UUID REFERENCES content_submissions(id) ON DELETE SET NULL,
                input JSONB NOT NULL,
                expected_assessment VARCHAR(40) NOT NULL,
                expected_concern VARCHAR(20),
                must_flag TEXT[] NOT NULL DEFAULT '{}',
                must_not_flag TEXT[] NOT NULL DEFAULT '{}',
                notes TEXT,
                created_by UUID REFERENCES users(id) ON DELETE SET NULL,
                created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW() NOT NULL
            );

            -- One row per Gemini call actually made, for usage limits (see services/ai/ai_usage_service.py).
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

        # The five brands content is filed under. The software products (ZooOffice, DeskFlex, Othership,
        # Zena Studios) roll up into "Software Brands"; Shaun's own posts are their own brand.
        await conn.execute("""
            INSERT INTO brands (code, name, sort_order, is_active)
            VALUES
                ('ZENATECH', 'ZenaTech', 10, true),
                ('ZENADRONE', 'ZenaDrone', 20, true),
                ('DAAS', 'Drone as a Service', 30, true),
                ('SOFTWARE', 'Software Brands', 40, true),
                ('CEO_THOUGHT_LEADERSHIP', 'Thought Leadership', 50, true)
            ON CONFLICT (code) DO UPDATE SET name = EXCLUDED.name, sort_order = EXCLUDED.sort_order, is_active = true;
        """)
        await _retire_product_brands(conn)

        if os.getenv("APP_ENV", "development").lower() != "production":
            await _seed_demo_social_accounts(conn)

        logger.info("Content submission schema setup successfully completed!")


# Individual software products that used to be separate brands; they're filed under "Software Brands" now.
RETIRED_PRODUCT_BRANDS = ["DESKFLEX", "OTHERSHIP", "ZOOOFFICE", "ZENASTUDIOS"]


async def _retire_product_brands(conn):
    """Hide the old product brands and move everything filed under them to SOFTWARE, and Shaun's accounts and posts
    to CEO_THOUGHT_LEADERSHIP. Safe to run on every start: once moved there's nothing left to change. The retired
    brands stay in the table (inactive) so nothing that pointed at them breaks."""
    retired = RETIRED_PRODUCT_BRANDS
    await conn.execute("UPDATE brands SET is_active = false WHERE code = ANY($1::text[]) AND is_active", retired)
    await conn.execute(
        "UPDATE content_submissions SET brand_code = 'SOFTWARE' WHERE brand_code = ANY($1::text[])", retired
    )
    # Brand access: anyone on a retired brand works on Software Brands instead.
    await conn.execute(
        """
        INSERT INTO user_brands (user_id, brand_code, assigned_by)
        SELECT DISTINCT user_id, 'SOFTWARE', NULL::uuid FROM user_brands WHERE brand_code = ANY($1::text[])
        ON CONFLICT (user_id, brand_code) DO NOTHING
        """,
        retired,
    )
    await conn.execute("DELETE FROM user_brands WHERE brand_code = ANY($1::text[])", retired)
    await conn.execute(
        """
        UPDATE compliance_rules
        SET brands = ARRAY(SELECT DISTINCT CASE WHEN b = ANY($1::text[]) THEN 'SOFTWARE' ELSE b END FROM unnest(brands) b)
        WHERE brands && $1::text[]
        """,
        retired,
    )
    # Connected accounts carry their brand in metadata: product accounts move to SOFTWARE, Shaun's to his brand.
    await conn.execute(
        """
        UPDATE oauth_connections
        SET connection_metadata = jsonb_set(connection_metadata, '{brand_code}', to_jsonb(
            CASE WHEN (connection_metadata->>'is_ceo')::boolean THEN 'CEO_THOUGHT_LEADERSHIP' ELSE 'SOFTWARE' END))
        WHERE (connection_metadata->>'brand_code' = ANY($1::text[]))
           OR ((connection_metadata->>'is_ceo')::boolean AND connection_metadata->>'brand_code' IS DISTINCT FROM 'CEO_THOUGHT_LEADERSHIP')
        """,
        retired,
    )
    # Posts going out through Shaun's accounts are his thought leadership.
    await conn.execute(
        """
        UPDATE content_submissions s SET brand_code = 'CEO_THOUGHT_LEADERSHIP'
        FROM oauth_connections c
        WHERE c.id = s.account_id AND (c.connection_metadata->>'is_ceo')::boolean
          AND s.brand_code IS DISTINCT FROM 'CEO_THOUGHT_LEADERSHIP'
        """
    )
    # Tables created by later setup steps; on a fresh database they don't exist yet (and hold nothing to move).
    if await conn.fetchval("SELECT to_regclass('public.messaging_context') IS NOT NULL"):
        await conn.execute(
            """
            UPDATE messaging_context
            SET brands = ARRAY(SELECT DISTINCT CASE WHEN b = ANY($1::text[]) THEN 'SOFTWARE' ELSE b END FROM unnest(brands) b)
            WHERE brands && $1::text[]
            """,
            retired,
        )
    if await conn.fetchval("SELECT to_regclass('public.approval_paths') IS NOT NULL"):
        await conn.execute("UPDATE approval_paths SET brand_code = 'SOFTWARE' WHERE brand_code = ANY($1::text[])", retired)


async def _seed_demo_social_accounts(conn):
    """Insert the demo organic social accounts that aren't there yet, flagged demo_seed in their metadata. Each is
    matched by its provider account id, so accounts added to this list later reach databases seeded earlier."""
    org_id = await conn.fetchval("SELECT id FROM organizations ORDER BY created_at ASC LIMIT 1")
    if not org_id:
        return

    accounts = [
        # provider, account name, handle, brand code, company code, is_ceo
        ("LINKEDIN_PAGE", "ZenaTech", "zenatech", "ZENATECH", "ZTG", False),
        ("X_TWITTER", "ZenaTech", "@ZenaTechInc", "ZENATECH", "ZTG", False),
        ("YOUTUBE", "ZenaTech", "@ZenaTech", "ZENATECH", "ZTG", False),
        ("TIKTOK", "ZenaTech", "@zenatech", "ZENATECH", "ZTG", False),
        ("FACEBOOK_PAGE", "ZenaTech", "ZenaTechInc", "ZENATECH", "ZTG", False),
        ("INSTAGRAM", "ZenaTech", "@zenatechinc", "ZENATECH", "ZTG", False),
        ("LINKEDIN_PAGE", "ZenaDrone", "zenadrone", "ZENADRONE", "ZDR", False),
        ("YOUTUBE", "ZenaDrone", "@ZenaDrone", "ZENADRONE", "ZDR", False),
        ("INSTAGRAM", "ZenaDrone", "@zenadrone", "ZENADRONE", "ZDR", False),
        ("FACEBOOK_PAGE", "ZenaDrone", "ZenaDroneOfficial", "ZENADRONE", "ZDR", False),
        ("FACEBOOK_PAGE", "Drone as a Service", "dronesasaservice", "DAAS", "ZDR", False),
        ("LINKEDIN_PAGE", "Drone as a Service", "drone-as-a-service", "DAAS", "ZDR", False),
        ("LINKEDIN_PAGE", "ZenaTech Software", "zenatech-software", "SOFTWARE", "ZTG", False),
        ("LINKEDIN_PROFILE", "Shaun – CEO", "shaun-ceo", "CEO_THOUGHT_LEADERSHIP", "ZTG", True),
        ("X_TWITTER", "Shaun – CEO", "@ShaunCEO", "CEO_THOUGHT_LEADERSHIP", "ZTG", True),
    ]
    result = await conn.execute(
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
        WHERE NOT EXISTS (
            SELECT 1 FROM oauth_connections c WHERE c.provider_account_id = 'demo-' || lower(a.provider) || '-' || a.handle
        )
        """,
        org_id,
        [a[0] for a in accounts],
        [a[1] for a in accounts],
        [a[2] for a in accounts],
        [a[3] for a in accounts],
        [a[4] for a in accounts],
        [a[5] for a in accounts],
    )
    added = int(result.split()[-1])
    if added:
        logger.info("Seeded %d demo social accounts for development.", added)


async def _main():
    from postgresql_db.database import create_pool
    await create_pool()
    try:
        await setup()
    finally:
        await close_pool()


if __name__ == "__main__":
    asyncio.run(_main())
