"""Brand downloads recover evicted files without exposing paths outside brand storage."""

from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import AsyncMock, Mock, patch

from fastapi import HTTPException

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from services.compliance.evaluation import brand_voice_files as files


class BrandDownloadTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.row = {"manual_path": "CEO_THOUGHT_LEADERSHIP/manual.pdf", "manual_name": "ZenaTech Brand Manual.pdf",
                    "logo_path": "CEO_THOUGHT_LEADERSHIP/logo.png"}

    async def resolve(self, asset="manual", hydrate=None):
        with patch.object(files, "fetch_one", AsyncMock(return_value=self.row)), \
             patch.object(files, "ensure_local_file", hydrate or Mock()):
            return await files.stored_file(self.root, "CEO_THOUGHT_LEADERSHIP", asset)

    async def test_missing_manual_is_restored_and_retains_download_name(self):
        def restore(path, category, relative):
            self.assertEqual(category, "brand_voice")
            self.assertEqual(relative, self.row["manual_path"])
            path.parent.mkdir(parents=True)
            path.write_bytes(b"%PDF-test")

        path, mime, name = await self.resolve(hydrate=Mock(side_effect=restore))
        self.assertEqual(path.read_bytes(), b"%PDF-test")
        self.assertEqual((mime, name), ("application/pdf", "ZenaTech Brand Manual.pdf"))

    async def test_existing_manual_needs_no_remote_download(self):
        path = self.root / self.row["manual_path"]
        path.parent.mkdir(parents=True)
        path.write_bytes(b"%PDF-local")
        remote = Mock()
        self.assertEqual((await self.resolve(hydrate=remote))[0], path)
        remote.assert_not_called()

    async def test_logo_uses_same_storage_recovery(self):
        def restore(path, category, relative):
            path.parent.mkdir(parents=True)
            path.write_bytes(b"png")

        path, mime, name = await self.resolve("logo", Mock(side_effect=restore))
        self.assertEqual(mime, "image/png")
        self.assertEqual(name, "CEO_THOUGHT_LEADERSHIP-logo.png")
        self.assertTrue(path.is_file())

    async def test_path_traversal_is_rejected_before_storage_access(self):
        self.row["manual_path"] = "../outside.pdf"
        remote = Mock()
        with self.assertRaises(HTTPException) as caught:
            await self.resolve(hydrate=remote)
        self.assertEqual(caught.exception.status_code, 404)
        remote.assert_not_called()

    async def test_missing_remote_file_has_actionable_error(self):
        with self.assertRaises(HTTPException) as caught:
            await self.resolve()
        self.assertEqual(caught.exception.status_code, 404)
        self.assertIn("upload the manual again", caught.exception.detail)


if __name__ == "__main__":
    unittest.main()
