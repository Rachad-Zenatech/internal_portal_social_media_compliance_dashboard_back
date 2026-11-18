import os
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from fastapi import HTTPException
from pydantic import ValidationError
from starlette.requests import Request

from models.shared.test_login_model import TestLoginRequest
from services.shared.auth.test_login_service import TEST_USERS, sign_in_test_user, test_login_allowed


class DummyLoginGateTests(unittest.TestCase):
    def test_only_explicit_local_development_is_enabled(self):
        for env, enabled, host, expected in [
            ("development", "true", "localhost", True),
            ("test", "true", "127.0.0.1", True),
            ("production", "true", "localhost", False),
            ("staging", "true", "localhost", False),
            ("development", "false", "localhost", False),
            ("development", "true", "example.com", False),
        ]:
            with self.subTest(env=env, enabled=enabled, host=host):
                request = Request({"type": "http", "scheme": "http", "path": "/", "headers": [],
                                   "server": (host, 8009)})
                with patch.dict(os.environ, {"APP_ENV": env, "DEV_TEST_LOGIN": enabled}, clear=True):
                    self.assertEqual(test_login_allowed(request), expected)

    def test_only_allowed_dummy_accounts_are_accepted(self):
        for account in TEST_USERS:
            self.assertEqual(TestLoginRequest.model_validate({"as": account}).as_, account)
        for account in ("creator", "admin", "someone@zenatech.com"):
            with self.subTest(account=account), self.assertRaises(ValidationError):
                TestLoginRequest.model_validate({"as": account})


class DummyAccountTests(unittest.IsolatedAsyncioTestCase):
    async def test_existing_user_is_used_without_role_or_account_creation(self):
        user = {"id": "dummy-id", "email": TEST_USERS["reviewer"][0], "is_super_admin": False}
        conn = MagicMock()
        conn.fetchrow = AsyncMock(return_value=user)
        pool = MagicMock()
        pool.acquire.return_value.__aenter__.return_value = conn
        with patch("services.shared.auth.test_login_service.get_pool", return_value=pool):
            self.assertEqual(await sign_in_test_user("reviewer"), user)
        sql, email, role = conn.fetchrow.call_args.args
        self.assertEqual((email, role), (TEST_USERS["reviewer"][0], "REVIEWER"))
        for condition in ("u.is_active = true", "u.deleted_at IS NULL", "u.is_super_admin = false",
                          "ur.is_active = true", "r.code = $2"):
            self.assertIn(condition, sql)
        self.assertNotIn("INSERT", sql)
        conn.execute.assert_not_called()

    async def test_unavailable_account_is_rejected(self):
        conn = MagicMock()
        conn.fetchrow = AsyncMock(return_value=None)
        pool = MagicMock()
        pool.acquire.return_value.__aenter__.return_value = conn
        with patch("services.shared.auth.test_login_service.get_pool", return_value=pool):
            with self.assertRaises(HTTPException) as caught:
                await sign_in_test_user("suggester")
        self.assertEqual(caught.exception.status_code, 404)

    async def test_arbitrary_account_cannot_reach_database(self):
        with patch("services.shared.auth.test_login_service.get_pool") as get_pool:
            with self.assertRaises(HTTPException):
                await sign_in_test_user("admin")
            get_pool.assert_not_called()
