"""Complete PDF sections remain available without AI or partial-extraction fields."""
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import AsyncMock, patch
from uuid import uuid4

import pymupdf
from fastapi import HTTPException
from services.compliance.evaluation import brand_manual_sections as service


def manual():
    document = pymupdf.open()
    for title in ("BRAND APPLICATIONS", "IMAGERY BOARD"):
        page = document.new_page()
        page.insert_text((40, 60), title, fontsize=24)
        page.insert_text((40, 110), "Full brand guidance, including #3456F5 and Century Gothic.", fontsize=10)
        page.insert_text((40, 800), "BRAND GUIDELINES", fontsize=8)
    document.new_page().draw_rect(pymupdf.Rect(50, 50, 300, 300), fill=(0, 0.3, 0.5))
    data = document.tobytes()
    document.close()
    return data


class ManualSectionsTests(unittest.TestCase):
    def test_every_page_is_grouped_by_its_heading_and_image_only_pages_stay(self):
        sections = service.read_sections(manual())
        self.assertEqual([section.title for section in sections], ["BRAND APPLICATIONS", "IMAGERY BOARD", "Page 3"])
        self.assertEqual([section.page for section in sections], [1, 2, 3])
        self.assertIn("#3456F5", sections[0].text)
        self.assertIn("Century Gothic", sections[1].text)
        self.assertEqual(sections[2].text, "")

    def test_text_is_not_truncated_at_the_old_extraction_limit(self):
        document = pymupdf.open()
        page = document.new_page(width=600, height=12000)
        text = "Complete manual paragraph.\n" * 2600 + "FINAL GUIDELINE MUST REMAIN"
        self.assertGreaterEqual(page.insert_textbox(pymupdf.Rect(20, 20, 580, 11980), text, fontsize=3), 0)
        sections = service.read_sections(document.tobytes())
        document.close()
        self.assertGreater(len(sections[0].text), 60000)
        self.assertIn("FINAL GUIDELINE MUST REMAIN", sections[0].text)

    def test_original_artwork_is_returned_as_a_bounded_png(self):
        image = pymupdf.Pixmap(service.render_page(manual(), 3))
        self.assertGreater(image.width, 0)
        self.assertLessEqual(max(image.width, image.height), 1600)

    def test_invalid_page_numbers_are_not_silently_changed(self):
        for number in (0, 4):
            with self.subTest(number=number), self.assertRaises(HTTPException) as error:
                service.render_page(manual(), number)
            self.assertEqual(error.exception.status_code, 404)


class StoredManualTests(unittest.IsolatedAsyncioTestCase):
    async def test_existing_partial_manual_is_read_for_only_the_selected_brand(self):
        user_id = uuid4()
        with TemporaryDirectory() as folder:
            path = Path(folder) / "manual.pdf"
            path.write_bytes(manual())
            with patch.object(service.voices, "get_voice", AsyncMock(return_value={"status": "PARTIAL"})) as voice, \
                 patch.object(service.voices, "file_path", AsyncMock(return_value=(path, "application/pdf", "manual.pdf"))) as file:
                result = await service.sections("ZENATECH", user_id)
            self.assertEqual(len(result), 3)
            voice.assert_awaited_once_with("ZENATECH", user_id)
            file.assert_awaited_once_with("ZENATECH", "manual")

    async def test_missing_manual_preserves_not_found(self):
        with patch.object(service.voices, "get_voice", AsyncMock()), \
             patch.object(service.voices, "file_path", AsyncMock(side_effect=HTTPException(404))):
            with self.assertRaises(HTTPException) as error:
                await service.sections("ZENADRONE", uuid4())
        self.assertEqual(error.exception.status_code, 404)

    async def test_damaged_pdf_produces_an_explicit_error(self):
        with patch.object(service, "_manual", AsyncMock(return_value=b"broken PDF")):
            with self.assertRaises(HTTPException) as error:
                await service.sections("ZENATECH", uuid4())
        self.assertEqual(error.exception.status_code, 422)


class ManualRouteTests(unittest.TestCase):
    def test_full_manual_routes_can_be_registered(self):
        from tools.compliance.submissions.context_router import router
        paths = {route.path for route in router.routes}
        self.assertIn("/messaging-context/brand-voices/{brand_code}/manual/sections", paths)
        self.assertIn("/messaging-context/brand-voices/{brand_code}/manual/pages/{page}", paths)
