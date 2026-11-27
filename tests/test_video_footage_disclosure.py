import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from uuid import uuid4

import pytest
from pydantic import ValidationError
from models.compliance.submission_input_model import SubmissionCreate
from models.compliance.submission_model import SubmissionUpdate
from services.compliance.evaluation import video_footage_disclosure as disclosure
from services.compliance.media import video_disclaimer_scan
from services.compliance.submissions import video_footage_service
from services.compliance.submissions.submission_content_revision import content_changed


@pytest.mark.parametrize("text", [
    disclosure.DISCLAIMER,
    "Stock footage is used for illustrative purposes.",
    "Includes third party footage.",
    "This video includes AI generated content.",
    "THIRD-PARTY\nFOOTAGE",
])
def test_readable_disclosure_variants(text):
    assert disclosure.find_disclaimer(text)


@pytest.mark.parametrize("text", ["Our AI-powered drones improve surveying.", "No stock footage.", "Not AI-generated footage.", "This video does not contain any stock footage.", "", "Third-party contracts were signed."])
def test_unrelated_or_negated_text_does_not_satisfy_disclosure(text):
    assert not disclosure.find_disclaimer(text)


def material(segments=(), *, status="COMPLETED"):
    return SimpleNamespace(video={"id": uuid4(), "transcript_status": status, "storage_path": "test.mp4"},
                           segments=list(segments), visual_segments=[])


@pytest.mark.parametrize("answer", [None, False])
def test_no_declaration_or_no_answer_does_not_invent_a_footage_flag(answer):
    result = SimpleNamespace(violations=[])
    scan = AsyncMock()
    with patch.object(video_disclaimer_scan, "scan", scan):
        checked = asyncio.run(disclosure.check({"video_third_party_footage": answer}, material(), result))
    assert checked["status"] == "NOT_REQUIRED"
    assert not result.violations
    scan.assert_not_awaited()


def test_disclaimer_once_near_the_end_clears_without_more_reading():
    observed = material([{"source": "on_screen", "start_sec": 68.5, "text": disclosure.DISCLAIMER}])
    result = SimpleNamespace(violations=[])
    scan = AsyncMock()
    with patch.object(video_disclaimer_scan, "scan", scan):
        checked = asyncio.run(disclosure.check({"video_third_party_footage": True}, observed, result))
    assert checked["status"] == "FOUND" and checked["at_sec"] == 68.5
    assert not result.violations
    scan.assert_not_awaited()


@pytest.mark.parametrize("source", ["speech", "script", "caption"])
def test_disclosure_outside_on_screen_text_does_not_substitute(source):
    observed = material([{"source": source, "text": disclosure.DISCLAIMER}])
    result = SimpleNamespace(violations=[])
    with patch.object(video_disclaimer_scan, "scan", AsyncMock(return_value={"status": "NOT_FOUND"})):
        checked = asyncio.run(disclosure.check({"video_third_party_footage": True}, observed, result))
    assert checked["status"] == "NOT_FOUND"
    assert len(result.violations) == 1
    finding = result.violations[0]
    assert "You declared" in finding.explanation
    assert finding.missing_disclaimers == [disclosure.DISCLAIMER]
    assert finding.image_file_ids == [observed.video["id"]]


def test_dense_scan_can_find_a_disclaimer_missed_by_sparse_samples():
    result = SimpleNamespace(violations=[])
    with patch.object(video_disclaimer_scan, "scan", AsyncMock(return_value={"status": "FOUND", "at_sec": 22})):
        checked = asyncio.run(disclosure.check({"video_third_party_footage": True}, material(), result))
    assert checked["status"] == "FOUND" and not result.violations


def test_unavailable_read_requests_verification_instead_of_claiming_certain_absence():
    result = SimpleNamespace(violations=[])
    with patch.object(video_disclaimer_scan, "scan", AsyncMock(return_value={"status": "UNAVAILABLE"})):
        asyncio.run(disclosure.check({"video_third_party_footage": True}, material(), result))
    assert "couldn't finish" in result.violations[0].explanation


def test_pending_video_is_not_prematurely_flagged():
    result = SimpleNamespace(violations=[])
    checked = asyncio.run(disclosure.check({"video_third_party_footage": True}, material(status="PROCESSING"), result))
    assert checked["status"] == "PENDING" and not result.violations


def test_explicit_no_round_trips_and_changed_answers_invalidate_checks():
    create = SubmissionCreate(title="Video", content_type="VIDEO", video_third_party_footage=False)
    assert create.video_third_party_footage is False
    update = SubmissionUpdate(video_third_party_footage=False).model_dump(exclude_unset=True)
    fields = video_footage_service.update_fields({"video_third_party_footage": True}, update)
    assert fields["video_third_party_footage"] is False and fields["video_footage_edited_at"]
    assert content_changed({"video_third_party_footage": True}, fields)
    assert video_footage_service.update_fields({"video_third_party_footage": False}, update) == {}
    assert video_footage_service.update_fields({}, {}) == {}
    with pytest.raises(ValidationError):
        SubmissionUpdate(video_third_party_footage="yes")


def test_cached_scan_is_reused_without_another_ocr_pass():
    cached = {"version": disclosure.VERSION, "status": "FOUND", "at_sec": 20}
    read = AsyncMock(return_value={"video_disclaimer_scan": cached})
    with patch.object(video_disclaimer_scan, "fetch_one", read), patch.object(video_disclaimer_scan, "read_frames") as ocr:
        checked = asyncio.run(video_disclaimer_scan.scan(material().video))
    assert checked == cached
    ocr.assert_not_called()
