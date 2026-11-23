"""Highlight the judged evidence rather than another occurrence of a broad rule trigger."""
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from uuid import uuid4

from models.compliance.compliance_model import ImageFindingRegion
from services.compliance.evaluation.finding_evidence import highlight_terms
from services.compliance.media import image_finding_service as images
from services.compliance.media import video_finding_service as videos


def finding(matched="ARMY ASSESSMENT", passage="ARMY ASSESSMENT"):
    return SimpleNamespace(
        flagged_terms=["army"], passage=passage,
        evaluation=SimpleNamespace(matched_text=matched), status="NEEDS_REVIEW",
        image_regions=[], image_file_ids=[], exception_terms=[], penalty=18, visual_evidence=False,
    )


class EvidenceTerms(unittest.TestCase):
    def test_prefers_judged_passage_to_broad_trigger(self):
        self.assertEqual(highlight_terms(finding()), ["ARMY ASSESSMENT"])

    def test_passage_disambiguates_a_short_model_quote(self):
        self.assertEqual(highlight_terms(finding("Army", "2-week U.S. Army assessment")), ["2-week U.S. Army assessment"])

    def test_does_not_replace_model_quote_with_unrelated_passage(self):
        self.assertEqual(highlight_terms(finding("Army assessment", "Source: U.S. Army")), ["Army assessment"])

    def test_legacy_findings_keep_their_triggers(self):
        self.assertEqual(highlight_terms({"flagged_terms": ["risk free"]}), ["risk free"])

    def test_legacy_passage_is_used_when_it_contains_the_trigger(self):
        self.assertEqual(highlight_terms({"passage": "Text in image 1: Army assessment", "flagged_terms": ["army"]}), ["Army assessment"])

    def test_large_unrelated_passage_does_not_hide_a_valid_trigger(self):
        self.assertEqual(highlight_terms({"passage": "x" * 500, "flagged_terms": ["risk free"]}), ["risk free"])

    def test_ambiguous_partial_word_cannot_validate_a_footer_box(self):
        text = images.normalized("Army assessment. Source: Army")
        self.assertFalse(images.overlaps_flagged_text("Army", text, ["Army assessment"]))
        self.assertTrue(images.overlaps_flagged_text("Army assessment", text, ["Army assessment"]))

    def test_full_passage_ocr_ignores_the_footer(self):
        words = [
            {"text": "army", "line": 1, "box": [100, 100, 130, 200]},
            {"text": "assessment", "line": 1, "box": [100, 210, 130, 400]},
            {"text": "army", "line": 2, "box": [900, 100, 930, 200]},
        ]
        found = images._ocr_regions(uuid4(), words, highlight_terms(finding()))
        self.assertEqual(len(found), 1)
        self.assertEqual(found[0].y, 0.1)
        self.assertEqual(found[0].w, 0.3)

    def test_all_occurrences_of_the_same_judged_passage_can_be_marked(self):
        words = [{"text": "risk", "line": n, "box": [n * 100, 0, n * 100 + 20, 50]}
                 for n in (1, 2)]
        self.assertEqual(len(images._ocr_regions(uuid4(), words, ["risk"])), 2)


class ImageEvidenceLocalization(unittest.IsolatedAsyncioTestCase):
    async def test_uncertain_clearance_discards_old_disclosure_box_without_relocalizing(self):
        flag = finding("Illustrative schematic, not assessed hardware.", "ARMY")
        flag.evaluation.source = "ADJUSTED"
        flag.evaluation.model_decision = "NO_VIOLATION"
        generated = await self.localize(flag, [], {"regions": []})
        generated.assert_not_awaited()
        self.assertEqual(flag.image_regions, [])
        self.assertEqual(flag.status, "NEEDS_REVIEW")

    async def localize(self, flag, words, response):
        with tempfile.TemporaryDirectory() as directory:
            Path(directory, "image.jpg").write_bytes(b"image")
            image = {"id": uuid4(), "storage_path": "image.jpg", "mime_type": "image/jpeg",
                     "image_text_status": "COMPLETED", "image_text": "Army assessment. Source: Army"}
            flag.image_regions = [ImageFindingRegion(file_id=image["id"], x=0.1, y=0.9, w=0.1, h=0.03)]
            generate = AsyncMock(return_value=SimpleNamespace(text=json.dumps(response)))
            with patch("services.compliance.media.transcription_service.STORAGE_ROOT", Path(directory).resolve()), \
                 patch("services.compliance.sentiment.engine._image_bytes", return_value=(b"image", "image/jpeg")), \
                 patch.object(images, "_ocr_word_boxes", return_value=words), \
                 patch.object(images.image_text_service, "_generate", generate):
                await images.locate_image_findings([image], [flag])
            return generate

    async def test_footer_ocr_does_not_prevent_vision_from_finding_headline(self):
        flag = finding()
        words = [{"text": "army", "line": 1, "box": [900, 100, 930, 200]}]
        model = await self.localize(flag, words, {"regions": [
            {"finding": 0, "text": "Army assessment", "box": [100, 100, 140, 500]},
        ]})
        self.assertEqual(len(flag.image_regions), 1)
        self.assertEqual(flag.image_regions[0].y, 0.1)
        self.assertIn('ARMY ASSESSMENT', str(model.await_args.args))
        self.assertEqual((flag.status, flag.penalty), ("NEEDS_REVIEW", 18))

    async def test_unresolvable_passage_leaves_no_guessed_or_old_footer_box(self):
        flag = finding()
        await self.localize(flag, [], {"regions": [
            {"finding": 0, "text": "Army", "box": [900, 100, 930, 200]},
        ]})
        self.assertEqual(flag.image_regions, [])
        self.assertEqual(flag.penalty, 18)

    async def test_video_does_not_mark_a_segment_with_only_the_generic_trigger(self):
        with tempfile.TemporaryDirectory() as directory:
            Path(directory, "video.mp4").write_bytes(b"video")
            video = {"id": uuid4(), "storage_path": "video.mp4"}
            with patch.object(videos, "STORAGE_ROOT", Path(directory).resolve()), \
                 patch.object(videos, "_frame_bytes", return_value=b"frame") as frames:
                await videos.locate_video_findings(video, [{"text": "Source: Army", "start_sec": 1, "end_sec": 2}], [finding()])
                frames.assert_not_called()
