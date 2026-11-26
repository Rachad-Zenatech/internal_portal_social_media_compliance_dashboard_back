"""SQL for the submission list, including previews and analysis status."""

from services.compliance.submissions import image_provenance_service, media_freshness
from services.compliance.submissions.review_hierarchy_service import manager_access_sql, reviewer_sees as _reviewer_sees


def list_query(accounts_column, pending_sql, unavailable_sql):
    return (
        """SELECT """ + manager_access_sql("$1") + """ AS manager_review_access, s.id, s.reference_code, s.title, s.content_type, s.brand_code, s.brand_codes, s.platforms, s.status,
               s.current_version, s.publish_at, s.publish_after_changes, s.approval_needed_by, s.submitted_at, s.created_at, s.updated_at, s.audience, s.audiences,
               s.due_at, s.due_kind, s.rule_sets, s.region, s.script_required, s.platform_captions,
               (EXISTS (SELECT 1 FROM content_submission_files sf WHERE sf.submission_id = s.id AND sf.kind = 'SCRIPT' AND sf.deleted_at IS NULL) OR EXISTS (SELECT 1 FROM content_script_versions sv WHERE sv.submission_id = s.id)) AS has_script,
               s.content_edited_at, """ + image_provenance_service.COLUMNS_SQL + media_freshness.REVISION_SQL + """ AS media_edited_at,
               (s.latest_check->>'checked_at')::timestamptz AS check_at,
               (SELECT max(a.created_at) FROM content_sentiment_analyses a
                 WHERE a.submission_id = s.id AND a.status = 'COMPLETED') AS sentiment_at,
               EXISTS (SELECT 1 FROM content_sentiment_analyses a WHERE a.submission_id = s.id
                        AND a.status IN ('WAITING_TRANSCRIPT', 'PENDING', 'PROCESSING')) AS sentiment_running,
               greatest(s.mood_checked_at,
                        (SELECT max(m.created_at) FROM public_mood_summaries m WHERE m.submission_id = s.id)) AS mood_at,
               EXISTS (SELECT 1 FROM content_submission_files mf WHERE mf.submission_id = s.id AND mf.kind = 'MAIN'
                        AND mf.deleted_at IS NULL) AS has_main,
               EXISTS (SELECT 1 FROM content_submission_files mf WHERE mf.submission_id = s.id AND mf.kind = 'MAIN'
                        AND mf.deleted_at IS NULL AND mf.mime_type LIKE 'video/%') AS main_is_video,
               CASE WHEN s.script_required AND s.content_type = 'VIDEO' THEN coalesce(s.script_status, 'DRAFT') END
                   AS script_status,
               CASE WHEN s.script_send->>'status' = 'PROCESSING'
                         AND (s.script_send->>'started_at')::timestamptz < NOW() - INTERVAL '10 minutes' THEN 'FAILED'
                    ELSE s.script_send->>'status' END AS script_send_status,
               CASE WHEN s.script_send->>'status' = 'PROCESSING'
                         AND (s.script_send->>'started_at')::timestamptz < NOW() - INTERVAL '10 minutes'
                    THEN 'Sending the script took too long. Send it again.' ELSE s.script_send->>'error' END AS script_send_error,
               left(s.caption_text, 600) AS caption_text,
               -- The script as the card's preview until a video is added: the uploaded file's text, else the last version sent.
               CASE WHEN s.script_required AND s.content_type = 'VIDEO' THEN coalesce(
                   (SELECT left(sf.doc_text, 500) FROM content_submission_files sf
                     WHERE sf.submission_id = s.id AND sf.kind = 'SCRIPT' AND sf.deleted_at IS NULL
                       AND sf.doc_text_status = 'COMPLETED' AND sf.doc_text <> ''
                     ORDER BY sf.created_at DESC LIMIT 1),
                   (SELECT left(sv.body, 500) FROM content_script_versions sv
                     WHERE sv.submission_id = s.id ORDER BY sv.version_number DESC LIMIT 1)
               ) END AS script_excerpt,
               s.latest_check->>'traffic_light' AS latest_traffic_light,
               CASE WHEN """ + pending_sql("s.latest_check") + """ THEN NULL ELSE (s.latest_check->>'score')::int END AS latest_score,
               CASE WHEN """ + pending_sql("s.latest_check") + " OR " + unavailable_sql("s.latest_check") + """ THEN NULL
                    ELSE COALESCE((s.latest_check->>'violations_count')::int,
                                  CASE WHEN jsonb_typeof(s.latest_check->'violations') = 'array'
                                       THEN jsonb_array_length(s.latest_check->'violations') END)
               END AS issue_count,
               """ + pending_sql("s.latest_check") + """ AS analysis_pending,
               """ + unavailable_sql("s.latest_check") + """ AS ai_unavailable,
               (SELECT count(*) FROM content_submission_files f
                 WHERE f.submission_id = s.id AND f.deleted_at IS NULL) AS file_count,
               (SELECT count(*) FROM content_review_comments rc
                 WHERE rc.submission_id = s.id AND rc.deleted_at IS NULL) AS comment_count,
               c.id AS acc_id, c.provider AS acc_provider, c.account_name AS acc_account_name,
               c.connection_metadata AS acc_connection_metadata,
               cov.id AS cov_id, cov.kind AS cov_kind, cov.original_name AS cov_original_name,
               cov.mime_type AS cov_mime_type, cov.size_bytes AS cov_size_bytes, cov.created_at AS cov_created_at,
               s.created_by, u.full_name AS created_by_name,"""
        + accounts_column
        + """
        FROM content_submissions s
        LEFT JOIN users u ON u.id = s.created_by
        LEFT JOIN oauth_connections c ON c.id = s.account_id
        -- Thumbnail: the main video/image, otherwise the first attached image.
        LEFT JOIN LATERAL (
            SELECT * FROM content_submission_files cf
            WHERE cf.submission_id = s.id AND cf.deleted_at IS NULL
              AND (cf.mime_type LIKE 'video/%' OR cf.mime_type LIKE 'image/%')
            ORDER BY (cf.kind = 'MAIN') DESC, cf.created_at ASC
            LIMIT 1
        ) cov ON true
        WHERE (s.created_by = $1 OR ($3 AND """ + _reviewer_sees("$1", "$4") + """))
          AND s.deleted_at IS NULL AND ($2::text IS NULL OR s.status = $2)
        -- What needs the creator comes first: changes to fix (a post's or its script's), then drafts to finish
        -- (including an approved script waiting for its video). Then what's waiting on someone else: content in
        -- review, and a script with a reviewer or being sent, which count the same however they're stored (a script
        -- in review is a draft underneath). Finished content goes last. Within each group the soonest deadline
        -- (its due date, else the date approval is needed by, else the publish date) is first.
        ORDER BY CASE
                     WHEN s.status = 'CHANGES_REQUESTED' OR s.script_status IN ('CHANGES_REQUESTED', 'EDITS_PROPOSED') THEN 0
                     WHEN s.status = 'DRAFT' AND coalesce(s.script_status, '') <> 'IN_REVIEW'
                          AND coalesce(s.script_send->>'status', '') <> 'PROCESSING' THEN 1
                     WHEN s.status = 'IN_REVIEW' OR s.script_status = 'IN_REVIEW'
                          OR s.script_send->>'status' = 'PROCESSING' THEN 2
                     ELSE 3
                 END,
                 coalesce(s.due_at, s.approval_needed_by, s.publish_at) ASC NULLS LAST,
                 s.updated_at DESC
        LIMIT 2000  -- High enough that nobody's list is cut short; the page shows it 25 at a time.
        """
    )
