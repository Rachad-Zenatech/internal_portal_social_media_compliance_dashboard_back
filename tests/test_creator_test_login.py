"""The test creator is available only through the existing local development gate."""
import os
import unittest
from unittest.mock import patch
from starlette.requests import Request
from models.shared.test_login_model import TestLoginRequest as LoginRequest
from services.shared.auth.test_login_service import TEST_USERS, test_login_allowed as allowed


class CreatorTestLoginTests(unittest.TestCase):
    def test_creator_uses_existing_creator_role(self):
        self.assertEqual(LoginRequest.model_validate({"as": "creator"}).as_, "creator")
        self.assertEqual(TEST_USERS["creator"], ("test.creator@example.com", "Test Creator", "USER"))

    def test_existing_production_and_remote_host_guards_apply_to_creator(self):
        for environment, host, enabled, expected in (
            ("development", "localhost", "true", True),
            ("production", "localhost", "true", False),
            ("development", "example.com", "true", False),
            ("development", "localhost", "false", False),
        ):
            with self.subTest(environment=environment, host=host, enabled=enabled):
                request = Request({"type": "http", "scheme": "http", "path": "/auth/test-login",
                                   "headers": [(b"host", host.encode())]})
                with patch.dict(os.environ, {"APP_ENV": environment, "DEV_TEST_LOGIN": enabled}):
                    self.assertEqual(allowed(request), expected)
