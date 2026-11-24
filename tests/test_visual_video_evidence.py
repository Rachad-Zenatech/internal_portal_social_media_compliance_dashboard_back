"""Visual findings require observed cues; missing frames are coverage, never rule breaches."""
import asyncio
import json
import subprocess
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from uuid import uuid4

import pytest

from models.compliance.compliance_model import ComplianceRule
from services.ai.core.bedrock_service import _extract_text_and_media
from services.compliance.evaluation import visual_review as review, visual_video_frames as frames


def rule(number="Rule 28"):
    return ComplianceRule(id=uuid4(), rule_number=number, title="Prohibited content", category="Visual",
                          summary="No prohibited visible references", created_at=datetime.now(timezone.utc),
                          updated_at=datetime.now(timezone.utc))


def answer(number="Rule 28", status="NEEDS_REVIEW", basis="COVERAGE_LIMITATION", evidence="No frames supplied"):
    return dict(rule_number=number, status=status, evidence_basis=basis, confidence=.5,
                evidence=evidence, reasoning="Only supplied material can be assessed", box=[], start_sec=0., end_sec=0.)


@pytest.mark.parametrize("number", ["Rule 27", "Rule 28", "Rule 29"])
def test_missing_frames_never_create_findings_or_charges(number):
    active = rule(number)
    parsed = review._parse(SimpleNamespace(text=json.dumps({"assessments": [answer(number)]})), [active])
    findings = []
    review._merge(findings, review._finding({"mime_type": "video/mp4"}, active, parsed[0], "mock"))
    assert parsed[0].status == "UNAVAILABLE"
    assert findings == []


def test_low_confidence_clearance_is_not_recast_as_a_content_warning():
    active = rule()
    parsed = review._parse(SimpleNamespace(text=json.dumps({"assessments": [
        answer(status="CLEARED", basis="NO_CONCERN", evidence="No prohibited wording observed")]})), [active])
    assert parsed[0].status == "UNAVAILABLE"


@pytest.mark.parametrize("status,confidence", [("VIOLATION", .95), ("NEEDS_REVIEW", .6)])
def test_actual_prohibited_content_and_ambiguous_cues_are_preserved(status, confidence):
    active = rule()
    data = answer(status=status, basis="OBSERVED_CONTENT", evidence="MAFIA on a licence plate")
    data.update(confidence=confidence, box=[100., 100., 200., 300.], start_sec=2., end_sec=2.)
    parsed = review._parse(SimpleNamespace(text=json.dumps({"assessments": [data]})), [active])
    finding = review._finding(dict(id=uuid4(), mime_type="video/mp4", original_name="test.mp4"), active, parsed[0], "mock")
    assert finding.status == status
    assert finding.passage == data["evidence"]
    assert finding.image_regions
    assert finding.penalty > 0 if status == "VIOLATION" else finding.penalty == 0


def test_review_records_missing_coverage_without_a_rule_finding(tmp_path):
    asset = dict(id=uuid4(), mime_type="video/mp4", storage_path="test.mp4", original_name="test.mp4")
    (tmp_path / "test.mp4").write_bytes(b"placeholder")
    req = SimpleNamespace(text="Inspection video", disclaimer_text="", audiences=[], brand_codes=[], region="",
                          sources=[], _visual_media=[asset])
    response = SimpleNamespace(text=json.dumps({"assessments": [answer()]}), model_version="mock")
    findings = []
    with patch("services.compliance.media.transcription_service.STORAGE_ROOT", tmp_path), \
         patch("services.ai.core.bedrock_service.is_bedrock_enabled", return_value=True), \
         patch.object(frames, "sample_parts", return_value=["actual samples"]), \
         patch.object(review.image_text_service, "_generate", AsyncMock(return_value=response)):
        coverage = asyncio.run(review.review(req, [rule()], findings))
    assert findings == []
    assert coverage[0]["status"] == "UNAVAILABLE"
    assert coverage[0]["input_mode"] == "SAMPLED_FRAMES"
    assert coverage[0]["unavailable_rules"] == ["Rule 28"]


def test_real_video_frames_survive_bedrock_serialization(tmp_path):
    video = tmp_path / "synthetic.mp4"
    subprocess.run([frames._ffmpeg(), "-hide_banner", "-loglevel", "error", "-f", "lavfi", "-i",
                    "color=c=red:s=320x240:d=2", "-c:v", "libx264", "-pix_fmt", "yuv420p", str(video)],
                   capture_output=True, check=True, timeout=20)
    parts = frames.sample_parts(video)
    blocks, _ = _extract_text_and_media([parts, "Review samples"])
    images = [block["image"] for block in blocks if "image" in block]
    labels = [block["text"] for block in blocks if "text" in block and block["text"].startswith("Video frame at")]
    assert 1 < len(images) <= frames.MAX_FRAMES
    assert len(images) == len(labels)
    assert all(image["source"]["bytes"].startswith(b"\xff\xd8") for image in images)


def test_failed_sampling_is_explicit_not_text_only():
    with patch.object(frames, "_ffmpeg", return_value="ffmpeg"), \
         patch.object(frames.subprocess, "run", return_value=SimpleNamespace(stderr=b"Invalid video")):
        with pytest.raises(ValueError, match="duration"):
            frames.sample_parts("invalid.mp4")
