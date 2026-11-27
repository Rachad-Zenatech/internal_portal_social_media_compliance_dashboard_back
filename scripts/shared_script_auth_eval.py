"""Service clock skew must not prevent script review or expire a person's portal session."""

import ast
import datetime
from pathlib import Path
import secrets
import types
import unittest
from unittest.mock import AsyncMock

from fastapi import HTTPException
import jwt


ROOT = Path(__file__).resolve().parents[1]


def load_symbol(path, name, namespace):
    tree = ast.parse((ROOT / path).read_text(encoding="utf-8"))
    symbol = next(node for node in tree.body if getattr(node, "name", None) == name)
    module = ast.Module(body=[symbol], type_ignores=[])
    exec(compile(ast.fix_missing_locations(module), str(path), "exec"), namespace)
    return namespace[name]


class SharedScriptAuthTests(unittest.IsolatedAsyncioTestCase):
    def test_service_token_tolerates_small_clock_skew_without_extending_expiry(self):
        now = datetime.datetime.now(datetime.timezone.utc)

        class AheadClock(datetime.datetime):
            @classmethod
            def now(cls, tz=None):
                return now + datetime.timedelta(seconds=3)

        mint = load_symbol("services/compliance/evaluation/shared_script_service.py", "_service_token", {
            "List": list, "datetime": types.SimpleNamespace(datetime=AheadClock, timezone=datetime.timezone,
                                                              timedelta=datetime.timedelta),
            "os": types.SimpleNamespace(getenv=lambda name: None), "secrets": secrets, "jwt": jwt,
            "SOURCE_SERVICE": "social-media", "JWT_SECRET": "test-secret-for-service-auth-only", "JWT_ALGORITHM": "HS256",
        })
        claims = jwt.decode(mint(["script_documents:read"]), "test-secret-for-service-auth-only", algorithms=["HS256"])
        self.assertTrue(claims["is_service_token"])
        self.assertEqual(claims["scopes"], ["script_documents:read"])
        self.assertEqual(claims["iat"], int((now - datetime.timedelta(seconds=27)).timestamp()))
        self.assertEqual(claims["exp"], int((now + datetime.timedelta(seconds=303)).timestamp()))

    async def test_upstream_errors_preserve_portal_session_and_validation_statuses(self):
        class ServiceError(Exception):
            def __init__(self, status):
                super().__init__("Upstream authentication or validation failed")
                self.status_code = status

        remote = types.SimpleNamespace(SharedScriptError=ServiceError, call=AsyncMock())
        call = load_symbol("services/compliance/evaluation/shared_script_review.py", "_call", {
            "List": list, "Dict": dict, "Any": object, "shared": remote, "HTTPException": HTTPException,
        })
        for upstream, expected in [(401, 502), (403, 403), (404, 404), (409, 409), (422, 422), (500, 502)]:
            with self.subTest(upstream=upstream):
                remote.call.side_effect = ServiceError(upstream)
                with self.assertRaises(HTTPException) as caught:
                    await call("POST", "/from-submission", ["script_documents:write"])
                self.assertEqual(caught.exception.status_code, expected)
        remote.call.side_effect = None
        remote.call.return_value = {"document_id": "script-1"}
        self.assertEqual(await call("GET", "/script-1", ["script_documents:read"]), {"document_id": "script-1"})


if __name__ == "__main__":
    unittest.main()
