"""Review access inherited by Content Managers through the saved role hierarchy."""
import os


def _is_dev() -> bool:
    return os.getenv("APP_ENV", "development").strip().lower() != "production"


def _admin_codes() -> str:
    return "('SUPER_ADMIN', 'ADMIN')" if _is_dev() else "('SUPER_ADMIN')"


def _super_or_dev_admin_sql(user_param: str) -> str:
    return (
        f"EXISTS (SELECT 1 FROM users u WHERE u.id = {user_param} AND u.deleted_at IS NULL AND ("
        f"u.is_super_admin OR EXISTS ("
        f"SELECT 1 FROM user_roles ur JOIN roles r ON r.id = ur.role_id "
        f"WHERE ur.user_id = u.id AND ur.is_active = true AND r.is_active = true "
        f"AND r.deleted_at IS NULL AND r.code IN {_admin_codes()})))"
    )


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
    """Named reviewers, their managers, and super admins (plus dev admins outside production) retain access."""
    base = (f"((({scope_param}::text[] IS NULL OR s.brand_codes && {scope_param}::text[]) AND {sent_to(user_param)})"
            f" OR {user_param} = ANY(s.reviewer_ids) OR {manager_access_sql(user_param)})")
    return f"({base} OR {_super_or_dev_admin_sql(user_param)})"


async def has_admin_access(user_id, *, include_dev_admin: bool = False) -> bool:
    """Super admins always have full visibility; ADMIN role gets full visibility in non-production."""
    if include_dev_admin and not _is_dev():
        return False
    from postgresql_db.database import fetch_one

    codes = ("SUPER_ADMIN", "ADMIN") if include_dev_admin else ("SUPER_ADMIN",)
    row = await fetch_one(
        """
        SELECT (u.is_super_admin OR EXISTS (
            SELECT 1 FROM user_roles ur JOIN roles r ON r.id = ur.role_id
            WHERE ur.user_id = u.id AND ur.is_active = true AND r.is_active = true
              AND r.deleted_at IS NULL AND r.code = ANY($2::text[])
        )) AS ok
        FROM users u WHERE u.id = $1 AND u.deleted_at IS NULL
        """,
        user_id, list(codes),
    )
    return bool(row and row["ok"])


async def is_dev_admin(user_id) -> bool:
    return await has_admin_access(user_id, include_dev_admin=True)
