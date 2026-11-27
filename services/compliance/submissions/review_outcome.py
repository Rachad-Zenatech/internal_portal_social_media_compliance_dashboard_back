"""Review outcome grouping is independent of the creator's remaining editing steps."""


def status_sql():
    return "(CASE WHEN s.status = 'CHANGES_REQUESTED' AND coalesce(s.publish_after_changes, false) THEN 'APPROVED' ELSE s.status END)"


# Held decisions are private to reviewers; creators see the release event instead.
ACTIVITY_ACTIONS = """
    CASE e.event_type
        WHEN 'SUBMITTED' THEN CASE WHEN (e.data->>'version')::int > 1 THEN 'resubmitted' ELSE 'submitted' END
        WHEN 'REVIEW_DECISION' THEN CASE
            WHEN e.data->>'handoff_pending' = 'true' AND NOT $2 THEN NULL
            ELSE CASE e.data->>'decision' WHEN 'APPROVE' THEN 'approved' ELSE 'requested changes on' END END
        WHEN 'REVIEW_HANDOFF_RELEASED' THEN CASE WHEN NOT $2 THEN
            CASE e.data->>'decision' WHEN 'APPROVE' THEN 'approved' ELSE 'requested changes on' END END
        WHEN 'APPROVAL_REOPENED' THEN 'reopened the approval of'
        WHEN 'RESCHEDULED' THEN 'rescheduled'
        WHEN 'FLAG_DISMISSED' THEN 'dismissed a compliance flag on'
        WHEN 'WITHDRAWN' THEN 'withdrew'
    END
"""
