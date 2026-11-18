"""Microsoft Entra user provisioning and claim mapping."""

import datetime
import os
from typing import Optional

from services.shared.core.cache_service import emit_cache_invalidation


def getCurrentUserFromMicrosoftClaims(token_info: dict) -> dict:
    """Extract normalized Microsoft identity claims from an ID token payload."""
    if not token_info:
        return {"oid": None, "tid": None, "sub": None, "email": "", "name": ""}
    email = (
        token_info.get("email")
        or token_info.get("preferred_username")
        or token_info.get("upn")
        or token_info.get("unique_name")
        or ""
    ).lower().strip()
    name = (
        token_info.get("name")
        or f"{token_info.get('given_name', '')} {token_info.get('family_name', '')}".strip()
        or token_info.get("displayName")
        or ""
    ).strip()
    return {
        "oid": token_info.get("oid") or token_info.get("sub"),
        "tid": token_info.get("tid"),
        "sub": token_info.get("sub"),
        "email": email,
        "name": name,
    }


async def _ensure_microsoft_user_roles(conn, user_id, *, is_initial_admin: bool, was_deleted: bool) -> None:
    """Ensure the Standard User role exists and assign initial/default roles for a Microsoft user."""
    user_role = await conn.fetchrow("""
        SELECT id FROM roles
        WHERE UPPER(code) = 'USER' OR UPPER(name) = 'STANDARD USER'
        ORDER BY (UPPER(code) = 'USER') DESC
        LIMIT 1
    """)
    if not user_role:
        user_role = await conn.fetchrow("""
            INSERT INTO roles (code, name, description, is_system_role, is_active)
            VALUES ('USER', 'Standard User', 'Standard user with view access', false, true)
            ON CONFLICT (code) DO UPDATE SET is_active = true
            RETURNING id
        """)

    if is_initial_admin:
        super_admin_role = await conn.fetchrow("SELECT id FROM roles WHERE code = 'SUPER_ADMIN'")
        if super_admin_role:
            await conn.execute(
                """
                INSERT INTO user_roles (user_id, role_id, assigned_at, is_active)
                VALUES ($1, $2, now(), true)
                ON CONFLICT (user_id, role_id)
                DO UPDATE SET is_active = true, assigned_at = now()
                """,
                user_id, super_admin_role["id"],
            )

    if was_deleted:
        await conn.execute("UPDATE user_roles SET is_active = false WHERE user_id = $1", user_id)
        if user_role:
            await conn.execute(
                """
                INSERT INTO user_roles (user_id, role_id, assigned_at, is_active)
                VALUES ($1, $2, now(), true)
                ON CONFLICT (user_id, role_id)
                DO UPDATE SET is_active = true, assigned_at = now()
                """,
                user_id, user_role["id"],
            )
    else:
        has_role = await conn.fetchval(
            "SELECT 1 FROM user_roles WHERE user_id = $1 AND is_active = true LIMIT 1",
            user_id,
        )
        if not has_role and user_role:
            await conn.execute(
                """
                INSERT INTO user_roles (user_id, role_id, assigned_at, is_active)
                VALUES ($1, $2, now(), true)
                ON CONFLICT (user_id, role_id)
                DO UPDATE SET is_active = true, assigned_at = now()
                """,
                user_id, user_role["id"],
            )


async def upsertMicrosoftUser(claims: dict, graph_profile: Optional[dict] = None):
    """Create or update the local user record for an authenticated Microsoft
    Entra identity. Automatically activates @zenatech.com users and assigns
    the default USER role.
    """
    from postgresql_db.database import get_pool
    from services.shared.auth.auth_service import invalidate_user_auth_cache

    department = (graph_profile.get("department") if graph_profile else None) or None
    job_title = (graph_profile.get("job_title") if graph_profile else None) or None
    account_enabled = graph_profile.get("account_enabled") if graph_profile else None
    graph_synced_at = datetime.datetime.now(datetime.timezone.utc) if graph_profile else None
    clean_email = (claims.get("email") or "").lower().strip()
    clean_name = (claims.get("name") or (graph_profile.get("display_name") if graph_profile else "") or "").strip()
    oid = claims.get("oid") or (graph_profile.get("object_id") if graph_profile else None)

    admin_emails_env = os.getenv("INITIAL_SUPER_ADMIN_EMAILS", "admin@example.com")
    admin_emails = [e.strip().lower() for e in admin_emails_env.split(",") if e.strip()]
    is_initial_admin = clean_email in admin_emails

    pool = get_pool()
    async with pool.acquire() as conn:
        user = await conn.fetchrow("""
            SELECT * FROM users
            WHERE (microsoft_object_id IS NOT NULL AND microsoft_object_id = $1)
               OR LOWER(TRIM(email)) = $2
            ORDER BY (microsoft_object_id = $1) DESC
            LIMIT 1
        """, oid, clean_email)

        was_deleted = bool(user and user.get("deleted_at") is not None)

        if user:
            await conn.execute("""
                UPDATE users SET
                    email = $1,
                    full_name = COALESCE(NULLIF($2, ''), full_name),
                    microsoft_object_id = COALESCE($3, microsoft_object_id),
                    microsoft_tenant_id = COALESCE($4, microsoft_tenant_id),
                    microsoft_subject_id = COALESCE($5, microsoft_subject_id),
                    auth_provider = 'microsoft',
                    sso_enabled = true,
                    department = COALESCE($6, department),
                    job_title = COALESCE($7, job_title),
                    graph_account_enabled = COALESCE($8, graph_account_enabled),
                    last_graph_sync = COALESCE($9, last_graph_sync),
                    is_active = CASE
                        WHEN $8::boolean IS FALSE THEN false
                        ELSE true
                    END,
                    is_super_admin = CASE
                        WHEN $11::boolean IS TRUE THEN true
                        WHEN deleted_at IS NOT NULL THEN false
                        ELSE is_super_admin
                    END,
                    deleted_at = NULL,
                    deleted_by = NULL,
                    last_login_at = now(),
                    last_sso_login_at = now(),
                    last_activity_at = now()
                WHERE id = $10
            """, clean_email, clean_name, oid, claims.get("tid"), claims.get("sub"),
                 department, job_title, account_enabled, graph_synced_at, user["id"], is_initial_admin)
            user = await conn.fetchrow("SELECT * FROM users WHERE id = $1", user["id"])
        else:
            user = await conn.fetchrow("""
                INSERT INTO users (
                    microsoft_object_id, microsoft_tenant_id, microsoft_subject_id,
                    email, full_name, auth_provider, sso_enabled, is_active, is_super_admin,
                    department, job_title, graph_account_enabled, last_graph_sync,
                    last_login_at, last_sso_login_at, last_activity_at, created_at
                ) VALUES ($1, $2, $3, $4, $5, 'microsoft', true, true, $10, $6, $7, $8, $9, now(), now(), now(), now())
                ON CONFLICT (email) DO UPDATE SET
                    full_name = COALESCE(NULLIF(EXCLUDED.full_name, ''), users.full_name),
                    microsoft_object_id = COALESCE(EXCLUDED.microsoft_object_id, users.microsoft_object_id),
                    microsoft_tenant_id = COALESCE(EXCLUDED.microsoft_tenant_id, users.microsoft_tenant_id),
                    microsoft_subject_id = COALESCE(EXCLUDED.microsoft_subject_id, users.microsoft_subject_id),
                    department = COALESCE(EXCLUDED.department, users.department),
                    job_title = COALESCE(EXCLUDED.job_title, users.job_title),
                    auth_provider = 'microsoft',
                    sso_enabled = true,
                    is_active = true,
                    is_super_admin = CASE WHEN $10::boolean IS TRUE THEN true WHEN users.deleted_at IS NOT NULL THEN false ELSE users.is_super_admin END,
                    deleted_at = NULL,
                    deleted_by = NULL,
                    last_login_at = now(),
                    last_sso_login_at = now(),
                    last_activity_at = now()
                RETURNING *
            """, oid, claims.get("tid"), claims.get("sub"), clean_email, clean_name,
                 department, job_title, account_enabled, graph_synced_at, is_initial_admin)

        if user:
            await _ensure_microsoft_user_roles(
                conn,
                user["id"],
                is_initial_admin=is_initial_admin,
                was_deleted=was_deleted,
            )
            invalidate_user_auth_cache(user["id"])
            await emit_cache_invalidation(scope="permissions", key=f"perms:{user['id']}")
            return dict(user)

        return None
