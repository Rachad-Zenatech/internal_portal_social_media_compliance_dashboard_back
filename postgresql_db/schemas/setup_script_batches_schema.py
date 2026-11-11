"""Script batches: a creator uploads one document holding several scripts, a reviewer reviews them all at once, and each
approved script becomes a post of its own for the creator to film and upload.

A batch holds the details every post from it shares (brand, platforms, audience, ...) as one JSON object, and its
scripts as items. An item becomes a real post only when a reviewer approves it (items.submission_id says which)."""

import logging

from postgresql_db.database import get_pool

logger = logging.getLogger(__name__)


async def setup() -> None:
    pool = get_pool()
    async with pool.acquire() as conn:
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS content_script_batches (
                id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
                title VARCHAR(300) NOT NULL,
                -- The uploaded document the scripts were split from.
                source_name VARCHAR(300),
                created_by UUID NOT NULL REFERENCES users(id),
                -- DRAFT (creator is setting it up) | PROCESSING (being checked, then sent) | IN_REVIEW (a reviewer has
                -- scripts to decide) | CHANGES_REQUESTED (the creator's turn) | DONE (every script approved).
                status VARCHAR(24) NOT NULL DEFAULT 'DRAFT',
                -- What every post from this batch shares: brand_codes, audiences, region, platforms, account_ids,
                -- rule_sets, approval_needed_by, reviewer_ids (the fields of a post's details).
                details JSONB NOT NULL DEFAULT '{}'::jsonb,
                -- Why the last send failed, for the creator.
                error TEXT,
                sent_at TIMESTAMP WITH TIME ZONE,
                created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW() NOT NULL,
                updated_at TIMESTAMP WITH TIME ZONE DEFAULT NOW() NOT NULL,
                deleted_at TIMESTAMP WITH TIME ZONE
            );
            CREATE INDEX IF NOT EXISTS idx_script_batches_creator ON content_script_batches(created_by) WHERE deleted_at IS NULL;
            CREATE INDEX IF NOT EXISTS idx_script_batches_status ON content_script_batches(status) WHERE deleted_at IS NULL;

            CREATE TABLE IF NOT EXISTS content_script_batch_items (
                id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
                batch_id UUID NOT NULL REFERENCES content_script_batches(id) ON DELETE CASCADE,
                position INT NOT NULL,
                title VARCHAR(300) NOT NULL,
                body TEXT NOT NULL,
                -- DRAFT | IN_REVIEW | CHANGES_REQUESTED | APPROVED (a post was made from it).
                status VARCHAR(24) NOT NULL DEFAULT 'DRAFT',
                check_result JSONB,
                review_note TEXT,
                reviewed_by UUID REFERENCES users(id),
                reviewed_at TIMESTAMP WITH TIME ZONE,
                -- The post made when the script was approved.
                submission_id UUID REFERENCES content_submissions(id),
                created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW() NOT NULL,
                updated_at TIMESTAMP WITH TIME ZONE DEFAULT NOW() NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_script_batch_items_batch ON content_script_batch_items(batch_id, position);
        """)
    logger.info("Script batch schema ready")
