"""Usage records for AI rate limits; created after the submission tables exist."""


async def ensure_ai_usage_events(conn) -> None:
    await conn.execute("""
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
