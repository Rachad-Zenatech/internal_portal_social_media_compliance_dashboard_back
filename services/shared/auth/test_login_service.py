"""Local sign-in for the existing, explicitly allowed dummy review accounts."""

import os

from fastapi import HTTPException, Request

from postgresql_db.database import get_pool


TEST_USERS = {
    "manager": ("test.manager@example.com", "Test Manager", "MANAGER"),
    "reviewer": ("test.reviewer@example.com", "Test Reviewer", "REVIEWER"),
    "reviewer2": ("test.reviewer2@example.com", "Test Reviewer 2", "REVIEWER"),
    "reviewer3": ("test.reviewer3@example.com", "Test Reviewer 3", "REVIEWER"),
    "suggester": ("test.suggester@example.com", "Test Suggester", "SUGGESTING_REVIEWER"),
}
_LOCAL_HOSTS = {"localhost", "127.0.0.1", "::1", "[::1]"}


def test_login_allowed(request: Request) -> bool:
    return (
        os.getenv("DEV_TEST_LOGIN", "false").strip().lower() in ("1", "true", "yes")
        and os.getenv("APP_ENV", "production").strip().lower() in ("development", "test", "local")
        and (request.url.hostname or "").lower() in _LOCAL_HOSTS
    )


async def sign_in_test_user(account: str):
    """Reuse an active dummy user and its permissions; never create or reactivate users."""
    if account not in TEST_USERS:
        raise HTTPException(status_code=404, detail="Test account not available")
    email, _, role = TEST_USERS[account]
    pool = get_pool()
    async with pool.acquire() as conn:
        user = await conn.fetchrow(
            """
            UPDATE users u SET last_login_at = now(), last_activity_at = now()
            WHERE u.email = $1 AND u.is_active = true AND u.deleted_at IS NULL
              AND u.is_super_admin = false
              AND EXISTS (
                  SELECT 1 FROM user_roles ur JOIN roles r ON r.id = ur.role_id
                  WHERE ur.user_id = u.id AND ur.is_active = true AND r.code = $2
              )
            RETURNING u.id, u.email, u.is_super_admin
            """,
            email, role,
        )
    if not user:
        raise HTTPException(status_code=404, detail="Test account not available")
    return user
