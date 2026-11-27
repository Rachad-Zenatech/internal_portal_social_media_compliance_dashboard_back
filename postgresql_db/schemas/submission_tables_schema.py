"""Submission table definitions, grouped by the feature records they store."""


async def _create_core_records(conn) -> None:
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
            -- A reviewer may allow the creator to publish the corrected version without another review round.
            ALTER TABLE content_submissions ADD COLUMN IF NOT EXISTS publish_after_changes BOOLEAN NOT NULL DEFAULT false;
            -- When the post's content last changed (a field, the caption, a file). A compliance check, sentiment analysis or
            -- public mood read from before then is out of date. Null means never changed since it was created.
            ALTER TABLE content_submissions ADD COLUMN IF NOT EXISTS content_edited_at TIMESTAMP WITH TIME ZONE;
            CREATE INDEX IF NOT EXISTS idx_content_submissions_due ON content_submissions(due_at)
                WHERE due_at IS NOT NULL AND deleted_at IS NULL;
            ALTER TABLE content_submissions ADD COLUMN IF NOT EXISTS deleted_by UUID REFERENCES users(id) ON DELETE SET NULL;
            -- The reviewers the creator sent it to. Empty: every reviewer on its brand. Otherwise only they (and its
            -- final approvers, added on submit) see it in the queue and get its notifications.
            ALTER TABLE content_submissions ADD COLUMN IF NOT EXISTS reviewer_ids UUID[] NOT NULL DEFAULT '{}';
            ALTER TABLE content_submissions ADD COLUMN IF NOT EXISTS context_picks JSONB NOT NULL DEFAULT '{}'::jsonb;
            ALTER TABLE content_submissions ADD COLUMN IF NOT EXISTS ai_generated_images BOOLEAN NOT NULL DEFAULT FALSE,
                ADD COLUMN IF NOT EXISTS image_provenance_edited_at TIMESTAMPTZ, ADD COLUMN IF NOT EXISTS ai_generated_images_confirmed BOOLEAN NOT NULL DEFAULT FALSE;
            ALTER TABLE content_submissions ADD COLUMN IF NOT EXISTS people_in_images BOOLEAN NOT NULL DEFAULT FALSE,
                ADD COLUMN IF NOT EXISTS people_in_images_confirmed BOOLEAN NOT NULL DEFAULT FALSE,
                ADD COLUMN IF NOT EXISTS people_in_images_detected BOOLEAN,
                ADD COLUMN IF NOT EXISTS employee_in_images BOOLEAN NOT NULL DEFAULT FALSE,
                ADD COLUMN IF NOT EXISTS employee_in_images_confirmed BOOLEAN NOT NULL DEFAULT FALSE,
                ADD COLUMN IF NOT EXISTS face_employee_flags JSONB NOT NULL DEFAULT '{}'::jsonb,
                ADD COLUMN IF NOT EXISTS face_auto_matched JSONB NOT NULL DEFAULT '{}'::jsonb,
                ADD COLUMN IF NOT EXISTS face_same_person JSONB NOT NULL DEFAULT '{}'::jsonb;

            CREATE TABLE IF NOT EXISTS employee_face_embeddings (
                id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
                submission_id UUID REFERENCES content_submissions(id) ON DELETE SET NULL,
                file_id UUID NOT NULL,
                face_index INTEGER NOT NULL,
                embedding JSONB NOT NULL,
                source_box JSONB,
                created_by UUID REFERENCES users(id) ON DELETE SET NULL,
                created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                UNIQUE (file_id, face_index)
            );

            -- Brands each user works on. A user with none listed (and every super admin) works on all brands.
    """)


async def _create_access_and_approvals(conn) -> None:
    await conn.execute("""
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
    """)


async def _create_media_metadata(conn) -> None:
    await conn.execute("""
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
            -- The main file's bytes as a hash, so the same post sent twice is recognised (see duplicate_service).
            ALTER TABLE content_submission_files ADD COLUMN IF NOT EXISTS content_sha256 CHAR(64);

            -- A post can go out through several connected accounts. account_id stays as the first (primary) one,
            -- which the calendar, screener and publish page use.
            -- A post can be for several brands. brand_code stays as the first (primary) one.
            ALTER TABLE content_submissions ADD COLUMN IF NOT EXISTS brand_codes TEXT[] NOT NULL DEFAULT '{}';
            UPDATE content_submissions SET brand_codes = ARRAY[brand_code]
            WHERE brand_code IS NOT NULL AND cardinality(brand_codes) = 0;
            -- A post can target several audiences. audience stays as the first (primary) one.
            ALTER TABLE content_submissions ADD COLUMN IF NOT EXISTS audiences TEXT[] NOT NULL DEFAULT '{}';
            UPDATE content_submissions SET audiences = ARRAY[audience]
            WHERE audience IS NOT NULL AND cardinality(audiences) = 0;
            ALTER TABLE content_submissions ADD COLUMN IF NOT EXISTS account_ids UUID[] NOT NULL DEFAULT '{}';
            UPDATE content_submissions SET account_ids = ARRAY[account_id]
            WHERE account_id IS NOT NULL AND cardinality(account_ids) = 0;

            -- Auto-generated speech transcript for video files (see services/compliance/media/transcription_service.py)
            ALTER TABLE content_submission_files ADD COLUMN IF NOT EXISTS transcript JSONB;
            ALTER TABLE content_submission_files ADD COLUMN IF NOT EXISTS transcript_status VARCHAR(20);
            ALTER TABLE content_submission_files ADD COLUMN IF NOT EXISTS transcript_error TEXT;
            ALTER TABLE content_submission_files ADD COLUMN IF NOT EXISTS transcript_updated_at TIMESTAMP WITH TIME ZONE;
            -- Text shown in an image (a headline on a graphic, figures, fine print), read once so the rulebook
            -- check can screen it like the caption (see services/compliance/media/image_text_service.py).
            ALTER TABLE content_submission_files ADD COLUMN IF NOT EXISTS image_text TEXT;
            ALTER TABLE content_submission_files ADD COLUMN IF NOT EXISTS image_text_status VARCHAR(20);
            ALTER TABLE content_submission_files ADD COLUMN IF NOT EXISTS image_text_error TEXT;
            ALTER TABLE content_submission_files ADD COLUMN IF NOT EXISTS image_text_version TEXT;
            ALTER TABLE content_submission_files ADD COLUMN IF NOT EXISTS image_text_details JSONB;
            ALTER TABLE content_submission_files ADD COLUMN IF NOT EXISTS person_detected BOOLEAN;
            -- Anonymous face boxes use Gemini's 0..1000 coordinate system; they let the creator review detections
            -- without attempting to identify anyone.
            ALTER TABLE content_submission_files ADD COLUMN IF NOT EXISTS face_boxes JSONB NOT NULL DEFAULT '[]'::jsonb;
            ALTER TABLE content_submission_files ADD COLUMN IF NOT EXISTS face_embeddings JSONB NOT NULL DEFAULT '[]'::jsonb;
            ALTER TABLE content_submission_files ADD COLUMN IF NOT EXISTS face_detection_version INTEGER NOT NULL DEFAULT 0;
            ALTER TABLE content_submission_files ADD COLUMN IF NOT EXISTS image_text_updated_at TIMESTAMP WITH TIME ZONE;
            -- Text of a script (or a blog's article file), read once so the rulebook check screens it like the
            -- caption (see services/compliance/media/document_text_service.py).
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

    """)


async def _create_review_history(conn) -> None:
    await conn.execute("""
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

    """)


async def _create_script_stage(conn) -> None:
    await conn.execute("""
            -- Script stage (see services/compliance/evaluation/script_review_service.py): a video post's script is reviewed and
            -- approved before the video is uploaded. Only posts created as videos since this existed need it, so
            -- drafts already in progress aren't blocked. script_status: DRAFT, IN_REVIEW, EDITS_PROPOSED (the
            -- reviewer changed the wording; the creator accepts or edits), CHANGES_REQUESTED, APPROVED.
            ALTER TABLE content_submissions ADD COLUMN IF NOT EXISTS script_required BOOLEAN NOT NULL DEFAULT false;
            ALTER TABLE content_submissions ADD COLUMN IF NOT EXISTS script_status VARCHAR(30);
            -- While a sent script is being read and scored in the background: {status: PROCESSING|FAILED, started_at, error}.
            ALTER TABLE content_submissions ADD COLUMN IF NOT EXISTS script_send JSONB;
            -- The creator's compliance check of the uploaded script before sending it: {key, rulebook_version, check,
            -- checked_at}. Reused when the script is sent if the wording, details and rulebook are unchanged.
            ALTER TABLE content_submissions ADD COLUMN IF NOT EXISTS script_precheck JSONB;
            -- When the creator last read the post's public mood (the step is done once they have, as in the editor).
            ALTER TABLE content_submissions ADD COLUMN IF NOT EXISTS mood_checked_at TIMESTAMP WITH TIME ZONE;
            -- A different caption for some of its platforms ({"LINKEDIN": "..."}); the others use caption_text.
            ALTER TABLE content_submissions ADD COLUMN IF NOT EXISTS platform_captions JSONB NOT NULL DEFAULT '{}'::jsonb;
            CREATE INDEX IF NOT EXISTS idx_content_submissions_script_status ON content_submissions(script_status)
                WHERE script_status IS NOT NULL AND deleted_at IS NULL;
            -- Each version of the script's text: the creator's upload or edit, or the reviewer's edit. The reviewer's
            -- decision on a version the creator sent is kept on that version.
            CREATE TABLE IF NOT EXISTS content_script_versions (
                id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
                submission_id UUID NOT NULL REFERENCES content_submissions(id) ON DELETE CASCADE,
                version_number INT NOT NULL,
                body TEXT NOT NULL,
                source VARCHAR(20) NOT NULL,
                file_id UUID REFERENCES content_submission_files(id) ON DELETE SET NULL,
                check_result JSONB,
                created_by UUID REFERENCES users(id) ON DELETE SET NULL,
                created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW() NOT NULL,
                review_decision VARCHAR(30),
                review_note TEXT,
                reviewed_by UUID REFERENCES users(id) ON DELETE SET NULL,
                reviewed_at TIMESTAMP WITH TIME ZONE,
                CONSTRAINT content_script_versions_unique UNIQUE (submission_id, version_number)
            );

            -- Reviewer decision on each review job (one job per submitted version).
            ALTER TABLE content_review_jobs ADD COLUMN IF NOT EXISTS decision VARCHAR(30);
            ALTER TABLE content_review_jobs ADD COLUMN IF NOT EXISTS decision_notes TEXT;
            ALTER TABLE content_review_jobs ADD COLUMN IF NOT EXISTS decided_by UUID REFERENCES users(id) ON DELETE SET NULL;
            ALTER TABLE content_review_jobs ADD COLUMN IF NOT EXISTS decided_at TIMESTAMP WITH TIME ZONE;
            ALTER TABLE content_review_jobs ALTER COLUMN status SET DEFAULT 'AWAITING_REVIEW';
            -- Rules the automated check can't test from text, which the approving reviewer confirmed by hand.
            ALTER TABLE content_review_jobs ADD COLUMN IF NOT EXISTS manual_checks JSONB;

            -- A suggesting reviewer's own suggestion on a script version: their edited wording and the notes they left on
            -- it. Only they see it for now; one per person and version.
            CREATE TABLE IF NOT EXISTS content_script_suggestions (
                submission_id UUID NOT NULL REFERENCES content_submissions(id) ON DELETE CASCADE,
                version_number INT NOT NULL,
                author_id UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                body TEXT NOT NULL,
                highlights JSONB NOT NULL DEFAULT '[]'::jsonb,
                created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW() NOT NULL,
                updated_at TIMESTAMP WITH TIME ZONE DEFAULT NOW() NOT NULL,
                PRIMARY KEY (submission_id, version_number, author_id)
            );

            -- Wording the reviewer highlighted or left a note on in a script version; the creator sees them too.
            CREATE TABLE IF NOT EXISTS content_script_highlights (
                submission_id UUID NOT NULL REFERENCES content_submissions(id) ON DELETE CASCADE,
                version_number INT NOT NULL,
                highlights JSONB NOT NULL DEFAULT '[]'::jsonb,
                updated_by UUID REFERENCES users(id) ON DELETE SET NULL,
                updated_at TIMESTAMP WITH TIME ZONE DEFAULT NOW() NOT NULL,
                PRIMARY KEY (submission_id, version_number)
            );

            -- Reviewers' calls on automated compliance flags, per version: a dismissal with its reason.
    """)


async def _create_flag_verdicts(conn) -> None:
    await conn.execute("""
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

            -- Reviewers' verdicts on findings, as labelled examples the contextual check learns from: the flag was
            -- right (CORRECT) or a false alarm (OVER_FLAGGED, which is every dismissal). One per rule per version.
            CREATE TABLE IF NOT EXISTS finding_verdicts (
                id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
                submission_id UUID NOT NULL REFERENCES content_submissions(id) ON DELETE CASCADE,
                version_number INT NOT NULL,
                rule_id UUID NOT NULL,
                rule_number VARCHAR(50) NOT NULL,
                verdict VARCHAR(20) NOT NULL,
                reason VARCHAR(40),
                note TEXT,
                passage TEXT,
                context TEXT,
                flagged_terms TEXT[] NOT NULL DEFAULT '{}',
                -- What the check said when the reviewer made the call (VIOLATION, NEEDS_REVIEW, ...), and what the AI
                -- decided when the rule is judged in context.
                finding_status VARCHAR(20),
                ai_decision VARCHAR(20),
                decided_by UUID REFERENCES users(id) ON DELETE SET NULL,
                decided_at TIMESTAMP WITH TIME ZONE DEFAULT NOW() NOT NULL,
                UNIQUE (submission_id, version_number, rule_id)
            );
            CREATE INDEX IF NOT EXISTS idx_finding_verdicts_rule ON finding_verdicts(rule_id);

            -- Review comments: what the reviewer wants changed (optionally pinned to a moment in a video), and the
            -- creator's replies. Tied to the version they were written on, so the history reads correctly.
    """)


async def _create_comments_and_delegations(conn) -> None:
    await conn.execute("""
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
            -- A comment on an area of an image: which image, and the box as fractions (0 to 1) of its width and height.
            ALTER TABLE content_review_comments ADD COLUMN IF NOT EXISTS file_id UUID;
            ALTER TABLE content_review_comments ADD COLUMN IF NOT EXISTS region_x REAL;
            ALTER TABLE content_review_comments ADD COLUMN IF NOT EXISTS region_y REAL;
            ALTER TABLE content_review_comments ADD COLUMN IF NOT EXISTS region_w REAL;
            ALTER TABLE content_review_comments ADD COLUMN IF NOT EXISTS region_h REAL;
            -- The comment this one replies to (quoted above it, like a chat reply)
            ALTER TABLE content_review_comments ADD COLUMN IF NOT EXISTS reply_to_id UUID
                REFERENCES content_review_comments(id) ON DELETE SET NULL;

            -- Review delegations: a reviewer hands a post in review to someone else. The delegate is added to the
            -- post's reviewers; the delegator stays on it, so they still see its status (and can still decide).
            CREATE TABLE IF NOT EXISTS content_review_delegations (
                id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
                submission_id UUID NOT NULL REFERENCES content_submissions(id) ON DELETE CASCADE,
                version_number INT NOT NULL DEFAULT 0,
                from_user_id UUID REFERENCES users(id) ON DELETE SET NULL,
                to_user_id UUID REFERENCES users(id) ON DELETE SET NULL,
                note TEXT,
                created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW() NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_content_review_delegations_submission
                ON content_review_delegations(submission_id, created_at);
            CREATE INDEX IF NOT EXISTS idx_content_review_delegations_from ON content_review_delegations(from_user_id);
            CREATE INDEX IF NOT EXISTS idx_content_review_delegations_to ON content_review_delegations(to_user_id);

            -- Gemini sentiment analysis (see services/compliance/analytics/sentiment_service.py). Advisory only.
            -- input is exactly what was analysed; content_hash lets an unchanged draft reuse its result.
    """)


async def _create_sentiment_records(conn) -> None:
    await conn.execute("""
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

            """)


async def setup_submission_tables(conn) -> None:
    """The original DDL batch remains atomic across the feature-specific statements."""
    async with conn.transaction():
        await _create_core_records(conn)
        await _create_access_and_approvals(conn)
        await _create_media_metadata(conn)
        await _create_review_history(conn)
        await _create_script_stage(conn)
        await _create_flag_verdicts(conn)
        await _create_comments_and_delegations(conn)
        await _create_sentiment_records(conn)
