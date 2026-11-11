import os
import unittest
from types import SimpleNamespace

# auth_router imports auth, which needs a signing secret; CI has no .env (same as tests/test_security_fixes.py).
os.environ.setdefault("JWT_SECRET", "test-only-secret-for-unit-tests-0123456789")
from unittest.mock import patch

from fastapi import HTTPException

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
        self.assertEqual({k: v[2] for k, v in auth_router.TEST_USERS.items()}, {"creator": "USER", "reviewer": "REVIEWER", "manager": "MANAGER", "reviewer2": "REVIEWER", "reviewer3": "REVIEWER"})
        self.assertTrue(all(v[0].endswith("@example.com") for v in auth_router.TEST_USERS.values()))


if __name__ == "__main__":
    unittest.main()
