import datetime
import json
import os
import time
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

# auth_router imports auth, which needs a signing secret; CI has no .env (same as tests/test_security_fixes.py).
os.environ.setdefault("JWT_SECRET", "test-only-secret-for-unit-tests-0123456789")

from fastapi import HTTPException

from services.shared.auth import auth_service
from tools.shared.auth import auth_router


def request(host="localhost"):
    return SimpleNamespace(url=SimpleNamespace(hostname=host), client=None, headers={})


class TestLoginGuardTests(unittest.IsolatedAsyncioTestCase):
    def allowed(self, env, host="localhost"):
        with patch.dict("os.environ", env, clear=False):
            return auth_router._test_login_allowed(request(host))

    def test_on_only_when_switched_on_in_development_at_localhost(self):
        self.assertTrue(self.allowed({"DEV_TEST_LOGIN": "true", "APP_ENV": "development"}))
        self.assertTrue(self.allowed({"DEV_TEST_LOGIN": "true", "APP_ENV": "development"}, host="127.0.0.1"))

    def test_off_unless_switched_on(self):
        with patch.dict("os.environ", {"APP_ENV": "development"}, clear=False):
            import os
            os.environ.pop("DEV_TEST_LOGIN", None)
            self.assertFalse(auth_router._test_login_allowed(request()))
        self.assertFalse(self.allowed({"DEV_TEST_LOGIN": "false", "APP_ENV": "development"}))

    def test_never_in_production(self):
        self.assertFalse(self.allowed({"DEV_TEST_LOGIN": "true", "APP_ENV": "production"}))

    def test_never_from_a_real_address(self):
        self.assertFalse(self.allowed({"DEV_TEST_LOGIN": "true", "APP_ENV": "development"}, host="portal.zenatech.com"))
        self.assertFalse(self.allowed({"DEV_TEST_LOGIN": "true", "APP_ENV": "development"}, host="10.0.0.5"))

    async def test_signing_in_is_refused_while_off(self):
        with patch.dict("os.environ", {"DEV_TEST_LOGIN": "false"}, clear=False):
            with self.assertRaises(HTTPException) as caught:
                await auth_router.test_login(auth_router.TestLoginRequest(**{"as": "reviewer"}), request())
        self.assertEqual(caught.exception.status_code, 404)

    def test_only_the_test_users_can_be_asked_for(self):
        with self.assertRaises(ValueError):
            auth_router.TestLoginRequest(**{"as": "admin"})
        self.assertEqual({k: v[2] for k, v in auth_router.TEST_USERS.items()}, {"creator": "USER", "reviewer": "REVIEWER", "manager": "MANAGER", "reviewer2": "REVIEWER", "reviewer3": "REVIEWER", "suggester": "SUGGESTING_REVIEWER"})
        self.assertTrue(all(v[0].endswith("@example.com") for v in auth_router.TEST_USERS.values()))

    async def test_signing_in_clears_stale_auth_cache_and_sets_cookie(self):
        user_id = uuid4()
        stale_time = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(hours=1)
        auth_service._user_auth_cache[user_id] = {
            "user": {"id": user_id, "last_activity_at": stale_time, "last_logout_at": None, "is_super_admin": False},
            "time": time.time(),
        }
        auth_service._last_activity_update[user_id] = time.time()

        tx = MagicMock()
        tx.__aenter__ = AsyncMock(return_value=None)
        tx.__aexit__ = AsyncMock(return_value=None)
        conn = MagicMock()
        conn.fetchrow = AsyncMock(return_value={"id": user_id, "is_super_admin": False})
        conn.execute = AsyncMock()
        conn.transaction = MagicMock(return_value=tx)
        acquire = MagicMock()
        acquire.__aenter__ = AsyncMock(return_value=conn)
        acquire.__aexit__ = AsyncMock(return_value=None)
        pool = MagicMock()
        pool.acquire = MagicMock(return_value=acquire)

        try:
            with (
                patch.dict("os.environ", {"DEV_TEST_LOGIN": "true", "APP_ENV": "development"}, clear=False),
                patch.object(auth_router, "get_pool", return_value=pool),
                patch.object(auth_router, "log_login_activity", new=AsyncMock()),
            ):
                resp = await auth_router.test_login(auth_router.TestLoginRequest(**{"as": "creator"}), request())
        finally:
            auth_service.invalidate_user_auth_cache(user_id)

        self.assertEqual(resp.status_code, 200)
        body = json.loads(resp.body)
        self.assertIn("token", body)
        self.assertIn(f"{auth_service.AUTH_COOKIE_NAME}={body['token']}", resp.headers.get("set-cookie", ""))
        self.assertNotIn(user_id, auth_service._user_auth_cache)
        self.assertNotIn(user_id, auth_service._last_activity_update)


if __name__ == "__main__":
    unittest.main()
