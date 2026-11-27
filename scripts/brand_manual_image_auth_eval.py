"""Browser link tokens may read manual artwork, while API and write access remain blocked."""

from pathlib import Path
import sys
import unittest
from unittest.mock import AsyncMock, patch
from uuid import uuid4

from fastapi import HTTPException
from starlette.requests import Request

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from services.shared.auth import auth_service as auth


class BrandManualImageAuthTests(unittest.IsolatedAsyncioTestCase):
    async def check_access(self, path, method="GET"):
        user_id = uuid4()
        token, _ = auth.create_link_token(user_id)
        request = Request({"type": "http", "method": method, "path": path, "headers": []})
        with patch.object(auth, "fetch_one", AsyncMock(return_value={"id": user_id})), \
             patch.object(auth, "_user_auth_cache", {}), patch.object(auth, "_last_activity_update", {}), \
             patch.object(auth, "current_user_id_ctx"):
            result = await auth.get_current_user_id_dependency(
                authorization=None, access_token=None, zenatech_token=None, query_token=token, request=request,
            )
        self.assertEqual(result, user_id)

    async def test_manual_artwork_accepts_browser_link_tokens(self):
        for brand, page in [("ZENATECH", 1), ("ZENATECH", 8), ("CEO_THOUGHT_LEADERSHIP", 12)]:
            with self.subTest(brand=brand, page=page):
                await self.check_access(f"/api/messaging-context/brand-voices/{brand}/manual/pages/{page}")

    async def test_original_manual_and_logo_remain_accessible(self):
        for asset in ["manual", "logo"]:
            with self.subTest(asset=asset):
                await self.check_access(f"/api/messaging-context/brand-voices/ZENATECH/{asset}")

    async def test_link_token_does_not_grant_api_or_invalid_page_access(self):
        for asset in ["manual/sections", "manual/pages/0", "manual/pages/-1", "manual/pages/1/edit", "read", ""]:
            with self.subTest(asset=asset):
                with self.assertRaises(HTTPException) as caught:
                    await self.check_access(f"/api/messaging-context/brand-voices/ZENATECH/{asset}")
                self.assertEqual(caught.exception.status_code, 401)

    async def test_link_token_cannot_write_to_manual_page_routes(self):
        for method in ["POST", "PUT", "PATCH", "DELETE"]:
            with self.subTest(method=method):
                with self.assertRaises(HTTPException) as caught:
                    await self.check_access("/api/messaging-context/brand-voices/ZENATECH/manual/pages/1", method)
                self.assertEqual(caught.exception.status_code, 401)


if __name__ == "__main__":
    unittest.main()
