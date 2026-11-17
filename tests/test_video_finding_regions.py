"""Video-frame regions are time-bound and only mark exact visible wording."""
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from uuid import uuid4

from models.compliance.compliance_model import ComplianceViolation, ImageFindingRegion
from services.compliance.media import video_finding_service as regions


def finding(terms=("risk free",)):
    return ComplianceViolation(
        rule_id=uuid4(), rule_number="24", title="No absolute claims", category="Claims",
        severity="HIGH", traffic_light="RED", flagged_terms=list(terms), explanation="Do not guarantee results",
    )


class VideoRegionModel(unittest.TestCase):
    def test_video_region_requires_a_complete_ordered_time_range(self):
        values = dict(file_id=uuid4(), x=0.2, y=0.3, w=0.4, h=0.1)
        self.assertEqual(ImageFindingRegion(**values, start_sec=1, end_sec=2).start_sec, 1)
        with self.assertRaises(ValueError):
            ImageFindingRegion(**values, start_sec=1)
        with self.assertRaises(ValueError):
            ImageFindingRegion(**values, start_sec=2, end_sec=1)


class VideoLocalization(unittest.IsolatedAsyncioTestCase):
    async def test_saves_a_tight_box_only_for_the_matching_moment(self):
        with tempfile.TemporaryDirectory() as directory:
            storage = Path(directory).resolve()
            (storage / "fixture.mp4").write_bytes(b"video")
            video = {"id": uuid4(), "storage_path": "fixture.mp4"}
            words = [
                {"text": "double", "line": (1, 1, 1), "box": [100, 100, 200, 500]},
                {"text": "risk", "line": (1, 1, 2), "box": [600, 200, 700, 430]},
                {"text": "free", "line": (1, 1, 2), "box": [600, 440, 700, 700]},
            ]
            flag = finding()
            with patch.object(regions, "STORAGE_ROOT", storage), \
                 patch.object(regions, "_frame_bytes", return_value=b"frame") as frame, \
                 patch.object(regions, "_ocr_word_boxes", return_value=words):
                await regions.locate_video_findings(video, [{"start_sec": 1.0, "end_sec": 2.5, "text": "Double your money risk free"}], [flag])
            frame.assert_called_once()
            self.assertEqual(len(flag.image_regions), 1)
            box = flag.image_regions[0]
            self.assertEqual((box.file_id, box.start_sec, box.end_sec), (video["id"], 1.0, 2.5))
            self.assertEqual((box.x, box.y, box.w, box.h), (0.2, 0.6, 0.5, 0.1))

    async def test_does_not_extract_a_frame_when_the_segment_has_no_flagged_words(self):
        with tempfile.TemporaryDirectory() as directory:
            storage = Path(directory).resolve()
            (storage / "fixture.mp4").write_bytes(b"video")
            video = {"id": uuid4(), "storage_path": "fixture.mp4"}
            with patch.object(regions, "STORAGE_ROOT", storage), patch.object(regions, "_frame_bytes") as frame:
                await regions.locate_video_findings(video, [{"start_sec": 0, "end_sec": 1, "text": "A careful claim"}], [finding()])
            frame.assert_not_called()
