"""Schema for creator requests that a reviewer dismiss a compliance finding."""


async def ensure_creator_flag_dismissal_requests(conn) -> None:
    await conn.execute("""
        CREATE TABLE IF NOT EXISTS creator_flag_dismissal_requests (
            id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            submission_id UUID NOT NULL REFERENCES content_submissions(id) ON DELETE CASCADE,
            version_number INT NOT NULL, rule_id UUID NOT NULL, rule_number VARCHAR(50) NOT NULL,
            flagged_terms TEXT[] NOT NULL DEFAULT '{}', traffic_light VARCHAR(20) NOT NULL,
            reason VARCHAR(40) NOT NULL, note TEXT, requested_by UUID REFERENCES users(id) ON DELETE SET NULL,
            requested_at TIMESTAMP WITH TIME ZONE DEFAULT NOW() NOT NULL,
            status VARCHAR(20) NOT NULL DEFAULT 'PENDING',
            UNIQUE (submission_id, version_number, rule_id)
        );
        CREATE INDEX IF NOT EXISTS idx_creator_flag_dismissal_requests_submission
            ON creator_flag_dismissal_requests(submission_id);
    """)
