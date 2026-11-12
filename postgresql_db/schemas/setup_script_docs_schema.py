"""Script documents: scripts written in the platform from a template (not attached to a post), reviewed together by
several reviewers at once in a live editor.

A document's text and notes are shared Yjs documents: every change a person makes is kept as an update (script_document_updates)
so the editor can be rebuilt for anyone who opens it, and relayed live to everyone else. The plain text (body_html /
body_text) is a snapshot the editors save, used for lists, the compliance check and the history of versions.

    DRAFT -> IN_REVIEW (reviewers co-edit, each checks in) -> LEVEL2_REVIEW (Content Manager) -> APPROVED
                                                                       (each script in the document becomes a post)
                                  \\-> CHANGES_REQUESTED (back to the creator) -> IN_REVIEW again
"""

import logging

from postgresql_db.database import get_pool

logger = logging.getLogger(__name__)


async def setup() -> None:
    pool = get_pool()
    async with pool.acquire() as conn:
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS script_documents (
                id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
                title VARCHAR(300) NOT NULL,
                template_key VARCHAR(60) NOT NULL DEFAULT 'blank',
                created_by UUID NOT NULL REFERENCES users(id),
                -- DRAFT | IN_REVIEW | LEVEL2_REVIEW | CHANGES_REQUESTED | APPROVED
                status VARCHAR(24) NOT NULL DEFAULT 'DRAFT',
                body_html TEXT NOT NULL DEFAULT '',
                body_text TEXT NOT NULL DEFAULT '',
                -- brand_codes, audiences, region, platforms, rule_sets: what the compliance check judges it by.
                details JSONB NOT NULL DEFAULT '{}'::jsonb,
                -- How many times it has been sent for review; check-ins belong to a round.
                round INT NOT NULL DEFAULT 0,
                -- Whether the live editor has been given its starting text (from body_html) yet.
                seeded BOOLEAN NOT NULL DEFAULT FALSE,
                send_note TEXT,
                decision_note TEXT,
                decided_by UUID REFERENCES users(id),
                decided_at TIMESTAMP WITH TIME ZONE,
                created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW() NOT NULL,
                updated_at TIMESTAMP WITH TIME ZONE DEFAULT NOW() NOT NULL,
                deleted_at TIMESTAMP WITH TIME ZONE
            );
            CREATE INDEX IF NOT EXISTS idx_script_documents_creator ON script_documents(created_by) WHERE deleted_at IS NULL;
            CREATE INDEX IF NOT EXISTS idx_script_documents_status ON script_documents(status) WHERE deleted_at IS NULL;

            -- A creator's document: one or more scripts, each on its own page (its own script_documents row, with its own live
            -- editor, reviewers and review). The set holds what they share: the title and the details every post gets.
            CREATE TABLE IF NOT EXISTS script_sets (
                id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
                title VARCHAR(300) NOT NULL,
                created_by UUID NOT NULL REFERENCES users(id),
                details JSONB NOT NULL DEFAULT '{}'::jsonb,
                created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW() NOT NULL,
                updated_at TIMESTAMP WITH TIME ZONE DEFAULT NOW() NOT NULL,
                deleted_at TIMESTAMP WITH TIME ZONE
            );
            CREATE INDEX IF NOT EXISTS idx_script_sets_creator ON script_sets(created_by) WHERE deleted_at IS NULL;
            ALTER TABLE script_documents ADD COLUMN IF NOT EXISTS set_id UUID REFERENCES script_sets(id);
            ALTER TABLE script_documents ADD COLUMN IF NOT EXISTS position INT NOT NULL DEFAULT 1;
            CREATE INDEX IF NOT EXISTS idx_script_documents_set ON script_documents(set_id, position) WHERE deleted_at IS NULL;

            -- Who the creator sent it to. EDIT: edits the script live (and, with the review permission, approves it);
            -- VIEW: reads it and adds notes only.
            CREATE TABLE IF NOT EXISTS script_document_reviewers (
                document_id UUID NOT NULL REFERENCES script_documents(id) ON DELETE CASCADE,
                user_id UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                capacity VARCHAR(8) NOT NULL DEFAULT 'EDIT',
                position INT NOT NULL DEFAULT 0,
                added_at TIMESTAMP WITH TIME ZONE DEFAULT NOW() NOT NULL,
                PRIMARY KEY (document_id, user_id)
            );

            -- A reviewer saying they are finished for this round: APPROVE, CHANGES (request changes) or DONE (a suggesting
            -- reviewer, who has no say in the decision). Cleared whenever anyone changes the script or its notes after.
            CREATE TABLE IF NOT EXISTS script_document_checkins (
                document_id UUID NOT NULL REFERENCES script_documents(id) ON DELETE CASCADE,
                round INT NOT NULL,
                user_id UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                vote VARCHAR(10) NOT NULL,
                note TEXT,
                pass_to_manager BOOLEAN NOT NULL DEFAULT FALSE,
                checked_at TIMESTAMP WITH TIME ZONE DEFAULT NOW() NOT NULL,
                PRIMARY KEY (document_id, round, user_id)
            );

            CREATE TABLE IF NOT EXISTS script_document_events (
                id BIGSERIAL PRIMARY KEY,
                document_id UUID NOT NULL REFERENCES script_documents(id) ON DELETE CASCADE,
                round INT NOT NULL DEFAULT 0,
                user_id UUID REFERENCES users(id) ON DELETE SET NULL,
                user_name VARCHAR(200),
                kind VARCHAR(24) NOT NULL,
                detail JSONB NOT NULL DEFAULT '{}'::jsonb,
                created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW() NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_script_document_events_doc ON script_document_events(document_id, id);

            -- The script as it stood when it was sent and when it was decided, for the history and comparing versions.
            CREATE TABLE IF NOT EXISTS script_document_versions (
                id BIGSERIAL PRIMARY KEY,
                document_id UUID NOT NULL REFERENCES script_documents(id) ON DELETE CASCADE,
                round INT NOT NULL,
                label VARCHAR(60) NOT NULL,
                body_html TEXT NOT NULL,
                body_text TEXT NOT NULL,
                created_by UUID REFERENCES users(id) ON DELETE SET NULL,
                created_by_name VARCHAR(200),
                created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW() NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_script_document_versions_doc ON script_document_versions(document_id, id);

            -- When a document is approved each script in it becomes a post of the creator's (a video to attach), linked here.
            CREATE TABLE IF NOT EXISTS script_document_posts (
                document_id UUID NOT NULL REFERENCES script_documents(id) ON DELETE CASCADE,
                position INT NOT NULL,
                title VARCHAR(300) NOT NULL,
                body TEXT NOT NULL,
                submission_id UUID REFERENCES content_submissions(id),
                created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW() NOT NULL,
                PRIMARY KEY (document_id, position)
            );

            -- Every change to the live editor ("text") or its notes ("notes"), as Yjs updates, in order.
            CREATE TABLE IF NOT EXISTS script_document_updates (
                id BIGSERIAL PRIMARY KEY,
                document_id UUID NOT NULL REFERENCES script_documents(id) ON DELETE CASCADE,
                channel VARCHAR(8) NOT NULL,
                payload BYTEA NOT NULL,
                user_id UUID REFERENCES users(id) ON DELETE SET NULL,
                created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW() NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_script_document_updates_doc ON script_document_updates(document_id, id);
        """)
    logger.info("Script document schema ready")
