import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from uuid import uuid4

from models.compliance_model import ComplianceCheckResponse
from services import image_text_service, submission_service

SVC = "services.submission_service"
ITS = "services.image_text_service"


def submission_row(**overrides):
    row = {
        "id": uuid4(),
        "title": "Q3 update",
        "caption_text": "Read the full release on our newsroom.",
        "content_type": "IMAGE",
        "platforms": ["FACEBOOK"],
        "rule_sets": ["Public Company Disclosure"],
        "brand_code": "ZENADRONE",
        "audience": "GENERAL",
        "region": "Global",
    }
    row.update(overrides)
    return row


def image(text=None, status=None):
    return {"id": uuid4(), "image_text": text, "image_text_status": status}


class RulebookCheckReadsImageTextTests(unittest.IsolatedAsyncioTestCase):
    async def run_check(self, images, video=None):
        check = AsyncMock(return_value=ComplianceCheckResponse(is_compliant=True, rules_checked=3))
        schedule_missing = AsyncMock()

        # The check reads the post's documents (script, article) and its images with separate queries; these
        # tests are about images, so the post has no documents.
        async def fetch_all(sql, *args):
            return [] if "doc_text" in sql else images

        with (
            patch(f"{SVC}.fetch_one", new=AsyncMock(return_value=video)),
            patch(f"{SVC}.fetch_all", new=fetch_all),
            patch(f"{SVC}.check_compliance", new=check),
            patch(f"{SVC}.image_text_service.schedule_missing", new=schedule_missing),
            patch(f"{SVC}.document_text_service.schedule_missing", new=AsyncMock()),
        ):
            _, stored = await submission_service._run_check(submission_row())
        return check.await_args.args[0].text, stored, schedule_missing

    async def test_text_in_images_is_checked_with_the_caption_in_order(self):
        text, stored, _ = await self.run_check(
            [image("Estimates Q3 revenue of C$14.8M", "COMPLETED"), image("Illustrative only", "COMPLETED")]
        )

        self.assertIn("Read the full release on our newsroom.", text)
        self.assertLess(
            text.index("Text in image 1: Estimates Q3 revenue of C$14.8M"),
            text.index("Text in image 2: Illustrative only"),
        )
        self.assertEqual((stored["images_total"], stored["images_read"]), (2, 2))

    async def test_unread_images_are_read_now_and_reported_as_pending(self):
        unread = image()
        text, stored, schedule_missing = await self.run_check([unread, image("Done", "COMPLETED")])

        schedule_missing.assert_awaited_once_with([unread["id"]])
        self.assertNotIn("Text in image 1", text)
        self.assertEqual((stored["images_read"], stored["images_pending"], stored["images_failed"]), (1, 1, 0))

    async def test_unreadable_images_are_reported_not_hidden(self):
        _, stored, _ = await self.run_check([image(status="FAILED")])
        self.assertEqual((stored["images_total"], stored["images_read"], stored["images_failed"]), (1, 0, 1))

    async def test_a_post_without_images_reports_none(self):
        _, stored, schedule_missing = await self.run_check([])
        self.assertEqual(stored["images_total"], 0)
        schedule_missing.assert_awaited_once_with([])


class ImageTextReaderTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.storage = tempfile.TemporaryDirectory()
        self.root = Path(self.storage.name).resolve()
        (self.root / "graphic.png").write_bytes(b"png")

    def tearDown(self):
        self.storage.cleanup()

    async def read(self, generate):
        save = AsyncMock()
        recheck = AsyncMock()
        with (
            patch(f"{ITS}._claim", new=AsyncMock(return_value={
                "storage_path": "graphic.png", "mime_type": "image/png", "original_name": "graphic.png",
            })),
            patch(f"{ITS}._save", new=save),
            patch(f"{ITS}._generate", new=generate),
            patch("services.transcription_service.STORAGE_ROOT", self.root),
            patch("services.sentiment_service._image_bytes", return_value=(b"png", "image/png")),
            patch(f"{SVC}.recheck_after_image_text", new=recheck),
        ):
            await image_text_service.read_file(uuid4())
        return save, recheck

    async def test_read_text_is_saved_and_the_post_is_rechecked(self):
        response = SimpleNamespace(text='{"text": "  BIG   HEADLINE \\n\\n Estimates Q3  revenue "}')
        save, recheck = await self.read(AsyncMock(return_value=response))

        self.assertEqual(save.await_args.args[1], "COMPLETED")
        self.assertEqual(save.await_args.kwargs["text"], "BIG HEADLINE\nEstimates Q3 revenue")
        recheck.assert_awaited_once()

    async def test_a_failed_read_is_saved_as_failed_and_still_rechecks(self):
        save, recheck = await self.read(AsyncMock(side_effect=RuntimeError("quota")))

        self.assertEqual(save.await_args.args[1], "FAILED")
        self.assertIn("quota", save.await_args.kwargs["error"])
        recheck.assert_awaited_once()

    async def test_an_image_already_being_read_is_left_alone(self):
        recheck = AsyncMock()
        with (
            patch(f"{ITS}._claim", new=AsyncMock(return_value=None)),
            patch(f"{SVC}.recheck_after_image_text", new=recheck),
        ):
            await image_text_service.read_file(uuid4())
        recheck.assert_not_awaited()


class ImageTextHelperTests(unittest.TestCase):
    def test_only_a_posts_own_images_are_read(self):
        self.assertTrue(image_text_service.is_image_file("MAIN", "image/png"))
        self.assertTrue(image_text_service.is_image_file("IMAGE", "image/jpeg"))
        self.assertFalse(image_text_service.is_image_file("SUPPORTING", "image/png"))
        self.assertFalse(image_text_service.is_image_file("MAIN", "video/mp4"))


class ImageTextSchedulingTests(unittest.IsolatedAsyncioTestCase):
    async def test_only_unread_or_abandoned_images_are_scheduled(self):
        due = uuid4()
        with (
            patch(f"{ITS}.fetch_all", new=AsyncMock(return_value=[{"id": due}])) as fetch_all,
            patch(f"{ITS}.schedule") as schedule,
        ):
            await image_text_service.schedule_missing([due, uuid4()])

        sql = fetch_all.await_args.args[0]
        self.assertIn("image_text_status IS NULL", sql)
        self.assertEqual(list(schedule.call_args.args[0]), [due])

    async def test_nothing_to_schedule_makes_no_query(self):
        with patch(f"{ITS}.fetch_all", new=AsyncMock()) as fetch_all:
            await image_text_service.schedule_missing([])
        fetch_all.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
