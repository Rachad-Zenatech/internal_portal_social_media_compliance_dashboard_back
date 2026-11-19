"""Review access inherited by Content Managers through the saved role hierarchy."""


def managed_reviewers_sql(user_param: str) -> str:
    """Active users in descendant roles of this manager's active Content Manager role."""
    return f"""ARRAY(
        WITH RECURSIVE descendants AS (
            SELECT child.id FROM roles child
            JOIN roles manager ON manager.id = child.parent_role_id
            JOIN user_roles assigned ON assigned.role_id = manager.id
            JOIN users viewer ON viewer.id = assigned.user_id
            WHERE assigned.user_id = {user_param} AND assigned.is_active = true
              AND manager.code = 'MANAGER' AND manager.is_active = true AND manager.deleted_at IS NULL
              AND viewer.is_active = true AND viewer.deleted_at IS NULL
              AND child.is_active = true AND child.deleted_at IS NULL
            UNION
            SELECT child.id FROM roles child JOIN descendants parent ON child.parent_role_id = parent.id
            WHERE child.is_active = true AND child.deleted_at IS NULL
        )
        SELECT DISTINCT assigned.user_id FROM user_roles assigned
        JOIN descendants ON descendants.id = assigned.role_id
        JOIN users person ON person.id = assigned.user_id
        WHERE assigned.is_active = true AND person.is_active = true AND person.deleted_at IS NULL
          AND person.id <> {user_param}
    )"""


def manager_access_sql(user_param: str) -> str:
    return f"(s.reviewer_ids && {managed_reviewers_sql(user_param)})"


def sent_to(user_param: str = "$1") -> str:
    return f"(cardinality(s.reviewer_ids) = 0 OR {user_param} = ANY(s.reviewer_ids))"


def reviewer_sees(user_param: str, scope_param: str) -> str:
    """Named reviewers and their managers retain access across brand scopes."""
    return (f"((({scope_param}::text[] IS NULL OR s.brand_codes && {scope_param}::text[]) AND {sent_to(user_param)})"
            f" OR {user_param} = ANY(s.reviewer_ids) OR {manager_access_sql(user_param)})")
