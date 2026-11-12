"""Security fixes: caption HTML sanitizing, who may change super-admin access, and link tokens in URLs."""

import datetime
import os
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from uuid import uuid4

from fastapi import HTTPException

# auth_service refuses to load without a signing secret (CI has no .env). A throwaway one for the tests; a real
# secret from the environment wins.
os.environ.setdefault("JWT_SECRET", "test-only-secret-for-unit-tests-0123456789")

from services.shared.auth import auth_service
from services.shared.auth import rbac_service
from services.compliance.media.html_sanitizer import sanitize_caption_html  # noqa: E402


class CaptionSanitizerTests(unittest.TestCase):
    def test_keeps_editor_formatting(self):
        html = "<h2>Title</h2><p>Hi <b>there</b> <i>you</i></p><ul><li>one</li></ul><blockquote>q</blockquote><br>"
        self.assertEqual(sanitize_caption_html(html), html)

    def test_drops_scripts_handlers_and_styles(self):
        self.assertEqual(sanitize_caption_html('<img src=x onerror="alert(1)">ok'), "ok")
        self.assertEqual(sanitize_caption_html("<script>alert(1)</script>after"), "after")
        self.assertEqual(sanitize_caption_html('<p style="x" onclick="y()">a</p>'), "<p>a</p>")
        self.assertEqual(sanitize_caption_html("<svg><script>alert(1)</script></svg>t"), "t")

    def test_links_only_keep_safe_schemes(self):
        self.assertEqual(sanitize_caption_html('<a href="javascript:alert(1)">x</a>'), "<a>x</a>")
        self.assertEqual(sanitize_caption_html('<a href="java&#9;script:alert(1)">x</a>'), "<a>x</a>")
        self.assertIn('href="https://zenatech.com"', sanitize_caption_html('<a href="https://zenatech.com">s</a>'))

    def test_text_is_escaped_and_tags_closed(self):
        self.assertEqual(sanitize_caption_html("1 < 2 & 3"), "1 &lt; 2 &amp; 3")
        self.assertEqual(sanitize_caption_html("<b>open <i>nest"), "<b>open <i>nest</i></b>")
        self.assertIsNone(sanitize_caption_html(None))


class FakeConn:
    """Answers the guard's queries: is the actor a super admin, and is any target one."""

    def __init__(self, actor_super: bool, target_super: bool = False, super_role: bool = False):
        self.actor_super, self.target_super, self.super_role = actor_super, target_super, super_role

    async def fetchval(self, sql, *args):
        if "SELECT is_super_admin FROM users" in sql:
            return self.actor_super
        if "is_super_admin AND deleted_at IS NULL" in sql:
            return 1 if self.target_super else None
        if "code = 'SUPER_ADMIN'" in sql:
            return 1 if self.super_role else None
        return None


class AccountGuardTests(unittest.IsolatedAsyncioTestCase):
    async def test_non_super_admin_cannot_touch_a_super_admin(self):
        with self.assertRaises(PermissionError):
            await rbac_service._guard_account_change(FakeConn(False, target_super=True), uuid4(), [uuid4()], what="account")

    async def test_non_super_admin_cannot_change_own_roles_or_brands(self):
        me = uuid4()
        with self.assertRaises(PermissionError):
            await rbac_service._guard_account_change(FakeConn(False), me, [me], what="roles", self_allowed=False)

    async def test_ordinary_change_by_user_manager_is_allowed(self):
        await rbac_service._guard_account_change(FakeConn(False), uuid4(), [uuid4()], what="account")

    async def test_super_admin_may_do_anything(self):
        me = uuid4()
        await rbac_service._guard_account_change(FakeConn(True, target_super=True), me, [me], what="roles",
                                                 self_allowed=False)


def request(method: str, path: str):
    return SimpleNamespace(method=method, url=SimpleNamespace(path=path))


class LinkTokenTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.user_id = uuid4()
        self.user = {"id": self.user_id, "last_activity_at": None, "last_logout_at": None, "is_super_admin": False}
        auth_service._user_auth_cache.clear()

    async def call(self, req, *, bearer=None, query=None, cookie=None):
        with patch("services.shared.auth.auth_service.fetch_one", new=AsyncMock(return_value=self.user)):
            return await auth_service.get_current_user_id_dependency(
                authorization=f"Bearer {bearer}" if bearer else None, access_token=cookie, zenatech_token=None,
                query_token=query, request=req,
            )

    async def test_link_token_opens_media_by_url(self):
        token, _ = auth_service.create_link_token(self.user_id)
        path = f"/api/submissions/{uuid4()}/files/{uuid4()}"
        self.assertEqual(await self.call(request("GET", path), query=token), self.user_id)

    async def test_a_stale_cookie_does_not_stop_the_link_token_opening_media(self):
        token, _ = auth_service.create_link_token(self.user_id)
        path = f"/api/submissions/{uuid4()}/files/{uuid4()}"
        self.assertEqual(await self.call(request("GET", path), query=token, cookie="stale.or.foreign.token"), self.user_id)

    async def test_link_token_cannot_call_the_api(self):
        token, _ = auth_service.create_link_token(self.user_id)
        for req in (request("GET", "/api/me/permissions"), request("DELETE", f"/api/submissions/{uuid4()}/files/{uuid4()}")):
            with self.assertRaises(HTTPException) as e:
                await self.call(req, query=token)
            self.assertEqual(e.exception.status_code, 401)

    async def test_link_token_only_works_from_a_url(self):
        token, _ = auth_service.create_link_token(self.user_id)
        with self.assertRaises(HTTPException):
            await self.call(request("GET", "/api/notifications/stream"), bearer=token)

    async def test_session_token_is_refused_in_a_url(self):
        token = auth_service.create_access_token(self.user_id, False)
        with self.assertRaises(HTTPException) as e:
            await self.call(request("GET", "/api/notifications/stream"), query=token)
        self.assertEqual(e.exception.status_code, 401)
        self.assertEqual(await self.call(request("GET", "/api/me/permissions"), bearer=token), self.user_id)

    async def test_tokens_issued_before_logout_stay_dead(self):
        token = auth_service.create_access_token(self.user_id, False)
        self.user["last_logout_at"] = datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(seconds=5)
        with self.assertRaises(HTTPException):
            await self.call(request("GET", "/api/me/permissions"), bearer=token)


class ServiceAndForeignTokenTests(unittest.IsolatedAsyncioTestCase):
    """Only this app's own sign-in tokens sign a person in: never a service token, another app's token, or a token
    that names no user (the old fallbacks made those the oldest super admin, or a user found by email)."""

    def setUp(self):
        self.admin = {"id": uuid4(), "last_activity_at": None, "last_logout_at": None, "is_super_admin": True}
        auth_service._user_auth_cache.clear()

    def forge(self, **claims):
        import jwt
        now = datetime.datetime.now(datetime.timezone.utc)
        base = {"iss": auth_service.JWT_ISSUER, "aud": auth_service.JWT_AUDIENCE, "iat": now,
                "exp": now + datetime.timedelta(minutes=10)}
        return jwt.encode({**base, **claims}, auth_service.JWT_SECRET, algorithm=auth_service.JWT_ALGORITHM)

    async def refused(self, token):
        lookup = AsyncMock(return_value=self.admin)
        with patch("services.shared.auth.auth_service.fetch_one", new=lookup):
            with self.assertRaises(HTTPException) as e:
                await auth_service.get_current_user_id_dependency(
                    authorization=f"Bearer {token}", access_token=None, zenatech_token=None, query_token=None,
                    request=request("GET", "/api/me/permissions"),
                )
        self.assertEqual(e.exception.status_code, 401)
        return lookup

    async def test_a_service_token_is_not_a_user(self):
        from services.shared.auth.service_security_policy import generate_scoped_outbound_token
        await self.refused(generate_scoped_outbound_token("finance", ["read"]))
        # Even with this app's issuer and audience, a service token names a service, not a person.
        lookup = await self.refused(self.forge(sub="service:admin", is_service_token=True))
        lookup.assert_not_awaited()

    async def test_a_token_that_names_no_user_is_refused(self):
        lookup = await self.refused(self.forge(sub="someone", email="ceo@zenatech.com"))
        lookup.assert_not_awaited()

    async def test_another_apps_token_is_refused(self):
        await self.refused(self.forge(sub=str(self.admin["id"]), iss="app-template-auth"))
        await self.refused(self.forge(sub=str(self.admin["id"]), aud="finance-portal"))
        no_audience = self.forge(sub=str(self.admin["id"]))
        import jwt
        claims = jwt.decode(no_audience, auth_service.JWT_SECRET, algorithms=["HS256"], options={"verify_aud": False})
        claims.pop("aud")
        await self.refused(jwt.encode(claims, auth_service.JWT_SECRET, algorithm="HS256"))

    async def test_the_idle_logout_follows_the_database_not_the_token(self):
        member = {"id": uuid4(), "last_logout_at": None, "is_super_admin": False,
                  "last_activity_at": datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(hours=1)}
        token = self.forge(sub=str(member["id"]), is_super_admin=True)
        with (
            patch("services.shared.auth.auth_service.fetch_one", new=AsyncMock(return_value=member)),
            patch("postgresql_db.database.execute", new=AsyncMock()),
        ):
            with self.assertRaises(HTTPException) as e:
                await auth_service.get_current_user_id_dependency(
                    authorization=f"Bearer {token}", access_token=None, zenatech_token=None, query_token=None,
                    request=request("GET", "/api/me/permissions"),
                )
        self.assertEqual(e.exception.detail, "Logged out due to inactivity")


if __name__ == "__main__":
    unittest.main()
