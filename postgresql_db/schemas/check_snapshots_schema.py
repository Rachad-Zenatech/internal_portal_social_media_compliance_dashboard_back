"""Immutable inputs, results and human feedback for future compliance learning."""


async def ensure_check_snapshots(conn) -> None:
    await conn.execute("""
        CREATE TABLE IF NOT EXISTS compliance_check_snapshots (
            id UUID PRIMARY KEY,
            submission_id UUID,
            submission_version INT NOT NULL,
            captured_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            schema_version INT NOT NULL DEFAULT 1,
            input_sha256 TEXT NOT NULL,
            post_details JSONB NOT NULL,
            evaluated_input JSONB NOT NULL,
            check_result JSONB NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_check_snapshots_post
            ON compliance_check_snapshots(submission_id, captured_at);
        CREATE TABLE IF NOT EXISTS compliance_finding_snapshots (
            id UUID PRIMARY KEY,
            check_snapshot_id UUID NOT NULL REFERENCES compliance_check_snapshots(id),
            ordinal INT NOT NULL,
            rule_id UUID NOT NULL,
            rule_snapshot JSONB NOT NULL,
            finding JSONB NOT NULL,
            UNIQUE (check_snapshot_id, ordinal)
        );
        CREATE INDEX IF NOT EXISTS idx_finding_snapshots_check
            ON compliance_finding_snapshots(check_snapshot_id);
        CREATE TABLE IF NOT EXISTS compliance_check_post_links (
            check_snapshot_id UUID NOT NULL REFERENCES compliance_check_snapshots(id),
            submission_id UUID NOT NULL,
            submission_version INT NOT NULL,
            linked_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            PRIMARY KEY (check_snapshot_id, submission_id, submission_version)
        );
        CREATE INDEX IF NOT EXISTS idx_check_post_links_post
            ON compliance_check_post_links(submission_id);
        ALTER TABLE compliance_check_snapshots ALTER COLUMN submission_id DROP NOT NULL;
        CREATE TABLE IF NOT EXISTS compliance_feedback_events (
            id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            event_sequence BIGINT GENERATED ALWAYS AS IDENTITY UNIQUE,
            finding_snapshot_id UUID NOT NULL REFERENCES compliance_finding_snapshots(id),
            submission_version INT NOT NULL,
            verdict TEXT NOT NULL CHECK (verdict IN ('CORRECT', 'OVER_FLAGGED', 'RETRACTED')),
            resolution TEXT NOT NULL,
            selection_verified BOOLEAN NOT NULL DEFAULT FALSE,
            reason TEXT,
            note TEXT,
            decided_by UUID NOT NULL,
            decided_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
        );
        CREATE INDEX IF NOT EXISTS idx_feedback_events_finding
            ON compliance_feedback_events(finding_snapshot_id, decided_at);
        ALTER TABLE compliance_feedback_events ADD COLUMN IF NOT EXISTS
            event_sequence BIGINT GENERATED ALWAYS AS IDENTITY UNIQUE;
        ALTER TABLE compliance_feedback_events ADD COLUMN IF NOT EXISTS
            selection_verified BOOLEAN NOT NULL DEFAULT FALSE;

        ALTER TABLE finding_verdicts ADD COLUMN IF NOT EXISTS finding_snapshot_id UUID
            REFERENCES compliance_finding_snapshots(id);
        ALTER TABLE flag_reviews ADD COLUMN IF NOT EXISTS finding_snapshot_id UUID
            REFERENCES compliance_finding_snapshots(id);
        ALTER TABLE creator_flag_dismissal_requests ADD COLUMN IF NOT EXISTS finding_snapshot_id UUID
            REFERENCES compliance_finding_snapshots(id);
    """)
    await _protect_history(conn)


async def _protect_history(conn) -> None:
    # No foreign keys to live posts, rules or users: deleting a never-submitted draft or
    # retiring a rule/user must not delete or rewrite its frozen training history.
    await conn.execute("""
        CREATE OR REPLACE FUNCTION reject_compliance_history_mutation() RETURNS trigger AS $$
        BEGIN
            RAISE EXCEPTION 'Compliance learning history is append-only'
                USING ERRCODE = '55000';
        END;
        $$ LANGUAGE plpgsql;
    """)
    for table in ("compliance_check_snapshots", "compliance_finding_snapshots", "compliance_feedback_events",
                  "compliance_check_post_links"):
        await conn.execute(f"""
            DO $$ BEGIN
                IF NOT EXISTS (SELECT 1 FROM pg_trigger
                               WHERE tgname = '{table}_immutable'
                                 AND tgrelid = '{table}'::regclass) THEN
                    CREATE TRIGGER {table}_immutable BEFORE UPDATE OR DELETE ON {table}
                        FOR EACH ROW EXECUTE FUNCTION reject_compliance_history_mutation();
                END IF;
            END $$;
        """)
