"""Source trust: people's calls on the sources a post was checked against, so the checker learns which sites are worth
reading. One row per person, post, page and claim: "this claim matches / doesn't match what the page says". A row
with an empty claim_key is a call on the page itself: "a good source" or "a poor source". Only reviewers' calls count
towards a site's standing (see source_trust_service); everyone's are kept and shown."""

import logging

from postgresql_db.database import get_pool

logger = logging.getLogger(__name__)


async def setup() -> None:
    pool = get_pool()
    async with pool.acquire() as conn:
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS source_confirmations (
                id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
                submission_id UUID NOT NULL REFERENCES content_submissions(id) ON DELETE CASCADE,
                user_id UUID NOT NULL,
                -- Whether the person could review content when they made the call: only reviewers' calls count.
                by_reviewer BOOLEAN NOT NULL DEFAULT false,
                url TEXT NOT NULL,
                -- The site (lower case, without "www."): standing is kept per site.
                host TEXT NOT NULL,
                -- A hash of the claim as worded; '' for a call on the page itself.
                claim_key VARCHAR(64) NOT NULL DEFAULT '',
                claim TEXT,
                source_quote TEXT,
                -- The claim matches what the page says (true), or doesn't (false).
                matches BOOLEAN,
                -- The page is a good source (true) or a poor one (false).
                good_source BOOLEAN,
                created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW() NOT NULL,
                updated_at TIMESTAMP WITH TIME ZONE DEFAULT NOW() NOT NULL,
                UNIQUE (submission_id, user_id, url, claim_key)
            );
            CREATE INDEX IF NOT EXISTS idx_source_confirmations_host ON source_confirmations(host);
            CREATE INDEX IF NOT EXISTS idx_source_confirmations_submission ON source_confirmations(submission_id);
        """)
    logger.info("Source trust schema ready")
