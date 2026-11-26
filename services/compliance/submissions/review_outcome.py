"""Review outcome grouping is independent of the creator's remaining editing steps."""


def status_sql():
    return "(CASE WHEN s.status IN ('IN_REVIEW', 'CHANGES_REQUESTED') AND coalesce(s.publish_after_changes, false) THEN 'APPROVED' ELSE s.status END)"
