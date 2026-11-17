"""Image highlights use real bounded coordinates and preserve compliance results on failure."""
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from uuid import uuid4

from models.compliance.compliance_model import ComplianceViolation
from services.compliance.media import image_finding_service as regions


def finding(status="EVIDENCE_REQUIRED", terms=None):
    return ComplianceViolation(
        rule_id=uuid4(), rule_number="1", title="Source required", category="Claims",
        severity="MEDIUM", traffic_light="YELLOW", flagged_terms=terms or ["Project Agincourt"],
        explanation="Needs a source", status=status,
    )


class Coordinates(unittest.TestCase):
    def test_converts_gemini_order_to_image_fractions(self):
        file_id = uuid4()
        box = regions.region_from_box(file_id, [800, 100, 950, 900])
        self.assertEqual(box.file_id, file_id)
        self.assertEqual((box.x, box.y, box.w, box.h), (0.1, 0.8, 0.8, 0.15))

    def test_rejects_invalid_coordinates_instead_of_guessing(self):
        for box in (None, [], [1, 2, 3], [0, 0, 1001, 1000], [-1, 0, 1, 1],
                    [100, 0, 100, 100], [0, 500, 100, 400], [0, 0, float("nan"), 1],
                    [False, 0, 10, 10], [0, 0, "10", 10]):
            with self.subTest(box=box):
                self.assertIsNone(regions.region_from_box(uuid4(), box))

    def test_old_checks_default_to_no_regions(self):
        self.assertEqual(finding().image_regions, [])

    def test_ocr_words_make_a_tight_phrase_box_not_a_full_creative(self):
        file_id = uuid4()
        words = [
            {"text": "double", "line": (1, 1, 1), "box": [100, 100, 200, 900]},
            {"text": "risk", "line": (1, 1, 2), "box": [500, 200, 600, 450]},
            {"text": "free", "line": (1, 1, 2), "box": [500, 460, 600, 700]},
        ]
        found = regions._ocr_regions(file_id, words, ["risk free"])
        self.assertEqual(len(found), 1)
        self.assertEqual((found[0].x, found[0].y, found[0].w, found[0].h), (0.2, 0.5, 0.5, 0.1))


class Localization(unittest.IsolatedAsyncioTestCase):
    async def run_localization(self, findings, response=None, failure=None, image_text="Project Agincourt"):
        with tempfile.TemporaryDirectory() as directory:
            storage = Path(directory).resolve()
            (storage / "post.png").write_bytes(b"test image")
            image = dict(id=uuid4(), storage_path="post.png", mime_type="image/png",
                         image_text_status="COMPLETED", image_text=image_text)
            generate = AsyncMock(return_value=SimpleNamespace(text=json.dumps(response or {"regions": []})),
                                 side_effect=failure)
            with patch("services.compliance.media.transcription_service.STORAGE_ROOT", storage), \
                 patch("services.compliance.sentiment.engine._image_bytes", return_value=(b"test", "image/png")), \
                 patch.object(regions.image_text_service, "_generate", generate):
                await regions.locate_image_findings([image], findings)
            return image, generate

    async def test_warning_gets_highlight_for_the_correct_file(self):
        flag = finding()
        image, generate = await self.run_localization([flag], {"regions": [
            {"finding": 0, "text": "Project Agincourt", "box": [800, 100, 950, 900]},
        ]})
        self.assertEqual(len(flag.image_regions), 1)
        self.assertEqual(flag.image_regions[0].file_id, image["id"])
        self.assertEqual(flag.status, "EVIDENCE_REQUIRED")
        self.assertEqual(flag.penalty, 0)
        self.assertEqual(generate.await_args.kwargs["feature"], "IMAGE_FINDING_REGIONS")

    async def test_ocr_region_skips_model_localization_for_risk_free(self):
        flag = finding(terms=["risk free"])
        words = [
            {"text": "risk", "line": (1, 1, 1), "box": [500, 200, 600, 450]},
            {"text": "free", "line": (1, 1, 1), "box": [500, 460, 600, 700]},
        ]
        with patch.object(regions, "_ocr_word_boxes", return_value=words):
            _, generate = await self.run_localization([flag], image_text="DOUBLE YOUR MONEY RISK FREE")
        generate.assert_not_awaited()
        self.assertEqual((flag.image_regions[0].x, flag.image_regions[0].y,
                          flag.image_regions[0].w, flag.image_regions[0].h), (0.2, 0.5, 0.5, 0.1))

    async def test_multiline_phrase_can_have_multiple_boxes(self):
        flag = finding(terms=["Project Agincourt Pathway to the Autonomous Warfare Command"])
        response = {"regions": [
            {"finding": 0, "text": "Project Agincourt", "box": [800, 100, 850, 500]},
            {"finding": 0, "text": "Pathway to the Autonomous Warfare Command", "box": [850, 100, 900, 900]},
        ]}
        await self.run_localization([flag], response, image_text=flag.flagged_terms[0])
        self.assertEqual(len(flag.image_regions), 2)

    async def test_wrapped_headline_can_return_full_lines_around_the_flagged_phrase(self):
        flag = finding(terms=["procurement is failing"])
        response = {"regions": [
            {"finding": 0, "text": "U.S. Defense Procurement", "box": [180, 40, 250, 700]},
            {"finding": 0, "text": "Is Failing Our Warfighters.", "box": [250, 40, 330, 750]},
        ]}
        await self.run_localization([flag], response,
                                    image_text="U.S. Defense Procurement\nIs Failing Our Warfighters.")
        self.assertEqual(len(flag.image_regions), 2)

    async def test_an_unrelated_line_repeating_a_word_is_not_highlighted(self):
        flag = finding(terms=["procurement is failing"])
        response = {"regions": [
            {"finding": 0, "text": "Our procurement processes", "box": [600, 40, 680, 750]},
        ]}
        await self.run_localization([flag], response,
                                    image_text="Procurement is failing.\nOur procurement processes")
        self.assertEqual(flag.image_regions, [])

    async def test_rejects_unrelated_text_bad_indices_and_invalid_boxes(self):
        flag = finding()
        await self.run_localization([flag], {"regions": [
            {"finding": 0, "text": "Invented wording", "box": [0, 0, 100, 100]},
            {"finding": 10, "text": "Project Agincourt", "box": [0, 0, 100, 100]},
            {"finding": 0, "text": "Project Agincourt", "box": [-20, 0, 100, 100]},
        ]})
        self.assertEqual(flag.image_regions, [])

    async def test_rejects_a_fallback_box_that_covers_the_whole_creative(self):
        flag = finding(terms=["risk free"])
        await self.run_localization([flag], {"regions": [
            {"finding": 0, "text": "risk free", "box": [0, 0, 1000, 1000]},
        ]}, image_text="DOUBLE YOUR MONEY RISK FREE")
        self.assertEqual(flag.image_regions, [])

    async def test_cleared_or_absent_wording_does_not_call_gemini(self):
        for flag in (finding(status="CLEARED"), finding(terms=["Absent phrase"])):
            _, generate = await self.run_localization([flag])
            generate.assert_not_awaited()

    async def test_model_failure_preserves_the_rule_finding(self):
        flag = finding(status="VIOLATION")
        with self.assertLogs(regions.logger, level="WARNING"):
            await self.run_localization([flag], failure=RuntimeError("model unavailable"))
        self.assertEqual(flag.status, "VIOLATION")
        self.assertEqual(flag.flagged_terms, ["Project Agincourt"])
        self.assertEqual(flag.image_regions, [])


if __name__ == "__main__":
    unittest.main()
