"""Additive video-footage declaration and cached local disclosure scan."""


async def ensure_video_footage(conn):
    await conn.execute("""
        ALTER TABLE content_submissions
            ADD COLUMN IF NOT EXISTS video_third_party_footage BOOLEAN,
            ADD COLUMN IF NOT EXISTS video_footage_edited_at TIMESTAMPTZ;
        ALTER TABLE content_submission_files
            ADD COLUMN IF NOT EXISTS video_disclaimer_scan JSONB;
    """)
