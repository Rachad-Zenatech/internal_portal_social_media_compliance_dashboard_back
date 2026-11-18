"""Batch reads of current work and committed review decisions; no emails are sent here."""
from postgresql_db.database import fetch_all

SUBMISSIONS = """
SELECT s.*, EXISTS (SELECT 1 FROM oauth_connections ac
       WHERE ac.id = ANY(COALESCE(s.account_ids, '{}'::uuid[]) || ARRAY[s.account_id])
         AND COALESCE((ac.connection_metadata->>'is_ceo')::boolean, false)) AS ceo_account,
       'submission' AS source,
       CASE WHEN s.status = 'DRAFT' THEN 'Script' ELSE 'Post' END AS item_kind,
       e.id AS sent_event_id, e.created_at AS sent_at,
       EXISTS (SELECT 1 FROM content_submission_events p WHERE p.submission_id = s.id AND p.id < e.id
               AND (p.event_type IN ('SCRIPT_EDITED', 'SCRIPT_CHANGES_REQUESTED', 'APPROVAL_REOPENED')
                    OR (p.event_type = 'REVIEW_DECISION' AND p.data->>'decision' = 'REQUEST_CHANGES'))) AS resubmitted
FROM content_submissions s
LEFT JOIN LATERAL (
    SELECT id, created_at FROM content_submission_events e WHERE e.submission_id = s.id
      AND e.event_type = CASE WHEN s.status = 'DRAFT' THEN 'SCRIPT_SUBMITTED' ELSE 'SUBMITTED' END
    ORDER BY e.id DESC LIMIT 1
) e ON true
WHERE s.deleted_at IS NULL
  AND (s.status IN ('IN_REVIEW', 'CHANGES_REQUESTED')
       OR (s.status = 'DRAFT' AND s.script_required AND s.script_status IN ('IN_REVIEW', 'CHANGES_REQUESTED', 'EDITS_PROPOSED')))
  AND NOT (s.status = 'DRAFT' AND EXISTS (
      SELECT 1 FROM script_documents d WHERE d.deleted_at IS NULL AND d.details->>'source_submission_id' = s.id::text))
"""

DOCUMENTS = """
SELECT d.*, 'document' AS source, 'Script' AS item_kind,
       s.reference_code, s.due_at AS submission_due_at, s.due_kind AS submission_due_kind,
       s.approval_needed_by AS submission_approval_needed_by,
       e.id AS sent_event_id, e.created_at AS sent_at,
       EXISTS (SELECT 1 FROM script_document_events p WHERE p.document_id = d.id AND p.id < e.id
               AND p.kind = 'CHANGES_REQUESTED') AS resubmitted,
       ARRAY(SELECT r.user_id FROM script_document_reviewers r WHERE r.document_id = d.id AND r.capacity = 'EDIT') AS reviewer_ids,
       ARRAY(SELECT c.user_id FROM script_document_checkins c WHERE c.document_id = d.id AND c.round = d.round) AS checked_ids
FROM script_documents d
LEFT JOIN content_submissions s ON s.id::text = d.details->>'source_submission_id' AND s.deleted_at IS NULL
LEFT JOIN LATERAL (
    SELECT id, created_at FROM script_document_events e WHERE e.document_id = d.id AND e.kind IN ('SUBMITTED', 'LEVEL2')
    ORDER BY e.id DESC LIMIT 1
) e ON true
WHERE d.deleted_at IS NULL AND d.status IN ('IN_REVIEW', 'LEVEL2_REVIEW', 'CHANGES_REQUESTED')
  AND (NOT (d.details ? 'source_submission_id') OR s.id IS NOT NULL)
"""

USERS = """
SELECT u.id, u.email, pref.time_zone, COALESCE(NULLIF(u.full_name, ''), u.email) AS name, u.is_super_admin,
       ARRAY(SELECT ub.brand_code FROM user_brands ub WHERE ub.user_id = u.id) AS brands,
       ARRAY(SELECT DISTINCT g.code FROM user_roles ur JOIN roles r ON r.id = ur.role_id
             JOIN role_permission_groups pg ON pg.role_id = r.id JOIN permission_groups g ON g.id = pg.permission_group_id
             WHERE ur.user_id = u.id AND ur.is_active AND r.is_active AND r.deleted_at IS NULL) AS groups,
       EXISTS (SELECT 1 FROM user_roles ur JOIN roles r ON r.id = ur.role_id WHERE ur.user_id = u.id
               AND ur.is_active AND r.is_active AND r.deleted_at IS NULL AND r.code = 'SUPER_ADMIN') AS super_role
FROM users u LEFT JOIN review_email_preferences pref ON pref.user_id = u.id WHERE u.is_active AND u.deleted_at IS NULL AND u.email IS NOT NULL
"""

OUTCOMES = """
SELECT 'outcome:submission:' || e.id AS event_key, s.created_by AS user_id, s.title, s.reference_code,
       CASE WHEN e.event_type LIKE 'SCRIPT_%' THEN 'Script' ELSE 'Post' END AS item_kind,
       e.event_type AS event_kind, e.message, e.data AS detail, s.id::text AS entity_id,
       CASE WHEN e.event_type LIKE 'SCRIPT_%' THEN '/submissions/' || s.id || '/script'
            ELSE '/submissions/' || s.id END AS path
FROM content_submission_events e JOIN content_submissions s ON s.id = e.submission_id
CROSS JOIN review_email_state state
WHERE e.created_at >= state.activated_at AND s.deleted_at IS NULL
  AND e.event_type IN ('REVIEW_DECISION', 'APPROVAL_REOPENED', 'SCRIPT_APPROVED', 'SCRIPT_EDITED', 'SCRIPT_CHANGES_REQUESTED')
  AND e.actor_user_id IS DISTINCT FROM s.created_by
  AND NOT EXISTS (SELECT 1 FROM review_email_outbox o WHERE o.dedupe_key = 'outcome:submission:' || e.id)
UNION ALL
SELECT 'outcome:document:' || e.id, d.created_by, d.title, 'Script', 'Script', e.kind,
       COALESCE(e.detail->>'note', d.decision_note, ''), e.detail, d.id::text, '/script-docs/' || d.id
FROM script_document_events e JOIN script_documents d ON d.id = e.document_id
CROSS JOIN review_email_state state
WHERE e.created_at >= state.activated_at AND d.deleted_at IS NULL AND e.kind IN ('APPROVED', 'CHANGES_REQUESTED')
  AND e.user_id IS DISTINCT FROM d.created_by
  AND NOT EXISTS (SELECT 1 FROM review_email_outbox o WHERE o.dedupe_key = 'outcome:document:' || e.id)
LIMIT 500
"""


async def pending_rows() -> list[dict]:
    return [dict(r) for r in await fetch_all(SUBMISSIONS)] + [dict(r) for r in await fetch_all(DOCUMENTS)]


async def users() -> dict[str, dict]:
    return {str(r["id"]): dict(r) for r in await fetch_all(USERS)}


async def outcomes() -> list[dict]:
    return [dict(r) for r in await fetch_all(OUTCOMES)]
