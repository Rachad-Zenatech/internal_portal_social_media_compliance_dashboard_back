"""Enlarged image localization preserves the original image coordinate system."""
import io
import importlib.util
import json
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from services.compliance.media import image_finding_zoom as zoom


class ImageFindingZoomTests(unittest.IsolatedAsyncioTestCase):
    def test_crop_box_maps_back_to_original_image(self):
        self.assertEqual(zoom.original_box([100, 200, 300, 400], (400, 400, 1000, 1000), (1000, 1000)),
                         [460, 520, 580, 640])

    def test_invalid_or_whole_crop_boxes_are_not_highlights(self):
        for box in ([0, 0, 1000, 1000], [-1, 0, 20, 20], [0, 0, 1001, 20],
                    [1, 1, 1, 2], [0, 0, float("nan"), 20], [False, 0, 10, 10]):
            with self.subTest(box=box):
                self.assertIsNone(zoom.original_box(box, (0, 0, 100, 100), (200, 200)))

    async def test_zoom_replies_are_mapped_without_changing_finding_identity(self):
        response = SimpleNamespace(text=json.dumps({"regions": [
            {"finding": 2, "text": "MAFIA", "box": [100, 200, 300, 400]},
        ]}))
        with patch.object(zoom, "sections", return_value=[(b"crop", (400, 400, 1000, 1000), (1000, 1000))]), \
             patch.object(zoom.image_text_service, "_generate", AsyncMock(return_value=response)) as generate:
            result = await zoom.locate(b"image", "Locate flagged text.", {})
        self.assertEqual(result, [{"finding": 2, "text": "MAFIA", "box": [460, 520, 580, 640]}])
        self.assertEqual(generate.call_args.kwargs["feature"], "IMAGE_FINDING_REGIONS")

    @unittest.skipUnless(importlib.util.find_spec("PIL"), "Pillow is available in the application container")
    def test_sections_overlap_and_enlarge_the_image(self):
        from PIL import Image

        encoded = io.BytesIO()
        Image.new("RGB", (620, 618)).save(encoded, format="PNG")
        sections = zoom.sections(encoded.getvalue())
        self.assertEqual(len(sections), 4)
        self.assertGreater(sections[0][1][2], sections[2][1][0])
        with Image.open(io.BytesIO(sections[0][0])) as tile:
            self.assertGreater(tile.width, 620)

    async def test_failed_section_does_not_discard_other_section_results(self):
        response = SimpleNamespace(text='{"regions": [{"finding": 0, "text": "word", "box": [100,100,200,200]}]}')
        tiles = [(b"a", (0, 0, 600, 600), (1000, 1000)), (b"b", (400, 400, 1000, 1000), (1000, 1000))]
        with patch.object(zoom, "sections", return_value=tiles), \
             patch.object(zoom.image_text_service, "_generate", AsyncMock(side_effect=[RuntimeError("unavailable"), response])):
            result = await zoom.locate(b"image", "Locate word", {})
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["box"], [460, 460, 520, 520])
