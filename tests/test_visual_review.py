"""Visual review must reach scoring without keyword triggers or silent coverage gaps."""
import json
import asyncio
from functools import wraps
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from uuid import uuid4

import pytest

from models.compliance.compliance_model import ComplianceCheckRequest
from postgresql_db.schemas.setup_compliance_schema import BASE_RULES
from services.compliance.evaluation import compliance_service as cs, visual_review as vr
from services.compliance.media import image_finding_service as locations
from tests.test_baseline_rule_terms import rule_from_seed


def async_test(function):
    @wraps(function)
    def run(*args, **kwargs):
        return asyncio.run(function(*args, **kwargs))
    return run


def rules():
    return [rule_from_seed(seed) for seed in BASE_RULES if seed["rule_number"] in vr.RULE_NUMBERS]


def asset(mime="image/png"):
    return dict(id=uuid4(), original_name="graphic.png", storage_path="test.png", mime_type=mime,
                image_text_status="COMPLETED", image_text="")


def assessment(number="Rule 11", **changes):
    return dict(rule_number=number, status="VIOLATION", confidence=.96,
                evidence="Official service seal co-branded with the company logo",
                reasoning="The seal is used decoratively in the branded lockup.",
                box=[100., 200., 300., 400.], start_sec=0., end_sec=0., **changes)


def answer(**changes):
    items = []
    for rule in rules():
        data = assessment(rule.rule_number)
        if rule.rule_number != "Rule 11":
            data.update(status="CLEARED", evidence="", reasoning="No prohibited visual is present.", box=[])
        data.update(changes.get(rule.rule_number, {}))
        items.append(data)
    return SimpleNamespace(text=json.dumps({"assessments": items}), model_version="vision-test")


@asynccontextmanager
async def attached(_asset):
    yield "actual-image-part"


@async_test
async def test_no_ocr_keyword_needed_and_post_context_reaches_model():
    req = ComplianceCheckRequest(text="Industry commentary only. No endorsement is claimed.", audiences=["DEFENSE"])
    req._visual_media = [asset()]
    findings = []
    with patch.object(vr, "_asset_part", attached), patch.object(vr.image_text_service, "_generate", AsyncMock(return_value=answer())) as generate:
        coverage = await vr.review(req, rules(), findings)
    assert len(findings) == 1
    f = findings[0]
    assert (f.rule_number, f.penalty, f.status) == ("Rule 11", 35, "VIOLATION")
    assert f.evaluation.confidence == .96 and f.evaluation.model == "vision-test"
    assert f.flagged_terms == [] and f.image_regions[0].file_id == req._visual_media[0]["id"]
    assert coverage[0]["status"] == "COMPLETED" and len(coverage[0]["assessments"]) == 5
    prompt = generate.call_args.args[0][1]
    assert req.text in prompt and '"audiences": ["DEFENSE"]' in prompt
    assert all(r.summary in prompt for r in rules())


@pytest.mark.parametrize("number", sorted(vr.RULE_NUMBERS))
def test_each_visual_rule_uses_its_normal_penalty(number):
    rule = next(r for r in rules() if r.rule_number == number)
    f = vr._finding(asset(), rule, vr.Assessment(**assessment(number)), "vision-test")
    assert (f.penalty, f.traffic_light) == cs._penalty_and_light(rule)


@pytest.mark.parametrize("changes", [{"confidence": .8}, {"box": []}, {"evidence": ""}, {"box": [0., 0., 1001., 1001.]}])
def test_uncertain_or_ungrounded_visual_does_not_confirm_breach(changes):
    data = assessment()
    data.update(changes)
    rule = next(r for r in rules() if r.rule_number == "Rule 11")
    f = vr._finding(asset(), rule, vr.Assessment(**data), "vision-test")
    assert f.status == "NEEDS_REVIEW" and f.penalty == 0
    assert f.evaluation.source == "ADJUSTED"


@async_test
async def test_all_clear_remains_auditable_without_creating_issue_cards():
    req = ComplianceCheckRequest(text="Generic silhouettes and shields")
    req._visual_media = [asset()]
    response = answer(**{"Rule 11": {"status": "CLEARED", "box": [], "evidence": ""}})
    findings = []
    with patch.object(vr, "_asset_part", attached), patch.object(vr.image_text_service, "_generate", AsyncMock(return_value=response)):
        coverage = await vr.review(req, rules(), findings)
    assert findings == [] and coverage[0]["status"] == "COMPLETED"


@async_test
@pytest.mark.parametrize("response", [SimpleNamespace(text='{"assessments":[]}'), SimpleNamespace(text="invalid json"), None])
async def test_failure_or_incomplete_response_is_never_a_clean_pass(response):
    req = ComplianceCheckRequest(text="")
    req._visual_media = [asset()]
    findings = []
    mock = AsyncMock(return_value=response) if response else AsyncMock(side_effect=RuntimeError("quota"))
    with patch.object(vr, "_asset_part", attached), patch.object(vr.image_text_service, "_generate", mock):
        coverage = await vr.review(req, rules(), findings)
    assert coverage[0]["status"] == "UNAVAILABLE"
    assert findings == []


@async_test
async def test_no_media_or_no_scoped_visual_rules_does_not_call_ai():
    with patch.object(vr.image_text_service, "_generate", AsyncMock()) as generate:
        assert await vr.review(ComplianceCheckRequest(text="Army"), rules(), []) == []
        req = ComplianceCheckRequest(text="Army", _visual_media=[asset()])
        assert req._visual_media == []  # Public API input cannot select storage files.
        req._visual_media = [asset()]
        assert await vr.review(req, [], []) == []
        generate.assert_not_called()


def test_multiple_assets_merge_one_charge_and_preserve_regions():
    rule = next(r for r in rules() if r.rule_number == "Rule 11")
    findings = []
    for _ in range(2):
        vr._merge(findings, vr._finding(asset(), rule, vr.Assessment(**assessment()), "vision-test"))
    assert len(findings) == 1 and findings[0].penalty == 35
    assert len(findings[0].image_regions) == 2


@async_test
async def test_existing_text_localization_preserves_visual_boxes():
    image = asset()
    rule = next(r for r in rules() if r.rule_number == "Rule 11")
    finding = vr._finding(image, rule, vr.Assessment(**assessment()), "vision-test")
    before = list(finding.image_regions)
    await locations.locate_image_findings([image], [finding])
    assert finding.image_regions == before


def test_video_locations_are_timed_and_invalid_ranges_need_review():
    rule = next(r for r in rules() if r.rule_number == "Rule 11")
    data = assessment()
    data.update(start_sec=3., end_sec=5.)
    f = vr._finding(asset("video/mp4"), rule, vr.Assessment(**data), "vision-test")
    assert f.image_regions[0].start_sec == 3. and f.image_regions[0].end_sec == 5.
    data.update(end_sec=2.)
    f = vr._finding(asset("video/mp4"), rule, vr.Assessment(**data), "vision-test")
    assert f.status == "NEEDS_REVIEW" and f.image_regions == []


def test_an_uncertain_clear_is_kept_for_review():
    rule = next(r for r in rules() if r.rule_number == "Rule 11")
    data = assessment()
    data.update(status="CLEARED", confidence=.7, box=[], evidence="")
    f = vr._finding(asset(), rule, vr.Assessment(**data), "vision-test")
    assert f.status == "NEEDS_REVIEW" and f.evaluation.source == "ADJUSTED"


@pytest.mark.parametrize("change", ["duplicate", "nan", "unknown_status"])
def test_malformed_judgments_cannot_complete_a_visual_review(change):
    response = answer()
    data = json.loads(response.text)
    if change == "duplicate":
        data["assessments"][1] = data["assessments"][0]
    elif change == "nan":
        data["assessments"][0]["confidence"] = float("nan")
    else:
        data["assessments"][0]["status"] = "SAFE"
    response.text = json.dumps(data)
    with pytest.raises(ValueError):
        vr._parse(response, rules())


@async_test
@pytest.mark.parametrize("failure", ["none", "quota", "partial"])
async def test_visual_result_changes_the_saved_compliance_score_and_coverage(failure):
    unavailable = failure != "none"
    req = ComplianceCheckRequest(text="An industry perspective.", audiences=["DEFENSE"])
    req._visual_media = [asset(), asset()] if failure == "partial" else [asset()]
    generate = (AsyncMock(side_effect=[answer(), RuntimeError("quota")]) if failure == "partial" else
                AsyncMock(side_effect=RuntimeError("quota")) if unavailable else AsyncMock(return_value=answer()))
    with (
        patch.object(cs, "applicable_rules", AsyncMock(return_value=rules())),
        patch.object(cs, "_evaluate_rule", return_value=None),
        patch.object(cs, "_semantic_findings", AsyncMock(return_value=[])),
        patch.object(cs.source_check_service, "run", AsyncMock(return_value=SimpleNamespace(ai_failed=False, checks=[], manifest=None))),
        patch.object(cs.source_post_checks, "unsourced_image_findings", return_value=[]),
        patch.object(cs.synthetic_media_disclosure, "ensure_review"),
        patch.object(cs, "rulebook_version", AsyncMock(return_value="visual-test")),
        patch.object(vr, "_asset_part", attached),
        patch.object(vr.image_text_service, "_generate", generate),
    ):
        result = await cs.check_compliance(req)
    assert result.ai_unavailable is unavailable
    assert result.score == (100 if unavailable else 65)
    assert result.blocked is (not unavailable)
    assert not result.is_compliant
    if unavailable:
        assert result.violations == []
        assert (result.violations_count, result.review_count, result.evidence_count) == (0, 0, 0)
        assert result.traffic_light == "YELLOW"
    expected = ["COMPLETED", "UNAVAILABLE"] if failure == "partial" else ["UNAVAILABLE" if unavailable else "COMPLETED"]
    assert [c["status"] for c in result.visual_checks] == expected
    serialized = result.model_dump(mode="json")
    assert "_visual_media" not in req.model_dump() and "visual_checks" in serialized
    assert all(f.evaluation for f in result.violations)


@async_test
async def test_source_outage_hides_partial_breaches_and_image_disclosure_penalties():
    req = ComplianceCheckRequest(text="Product illustration", audiences=["DEFENSE"],
                                 content_parts=["imagery"], ai_generated_images=True)
    selected = rules() + [rule_from_seed(seed) for seed in BASE_RULES if seed["rule_number"] == "Rule 9"]
    rule = next(r for r in selected if r.rule_number == "Rule 11")
    confirmed = vr._finding(asset(), rule, vr.Assessment(**assessment()), "vision-test")
    with (
        patch.object(cs, "applicable_rules", AsyncMock(return_value=selected)),
        patch.object(cs, "_evaluate_rule", return_value=None),
        patch.object(cs, "_semantic_findings", AsyncMock(return_value=[confirmed])),
        patch.object(cs.source_check_service, "run", AsyncMock(return_value=cs.source_check_service.SourceCheckResult(ai_failed=True))),
        patch.object(cs, "rulebook_version", AsyncMock(return_value="outage-test")),
    ):
        result = await cs.check_compliance(req)
    assert result.ai_unavailable and not result.is_compliant
    assert result.violations == [] and not result.blocked
    assert (result.violations_count, result.review_count, result.evidence_count, result.cleared_count) == (0, 0, 0, 0)
    assert result.score == result.raw_score == 100
    assert result.traffic_light == "YELLOW"
    assert "could not complete" in result.recommendations[0]


@async_test
async def test_video_upload_is_deleted_even_if_review_raises(tmp_path):
    from services.compliance.media import transcription_service
    from services.compliance.sentiment import engine

    media = asset("video/mp4")
    path = tmp_path / media["storage_path"]
    with path.open("wb") as file:
        file.truncate(vr.MAX_VIDEO_BYTES + 1)
    uploaded = SimpleNamespace(uri="https://example.invalid/upload", name="files/test")
    with (
        patch.object(transcription_service, "STORAGE_ROOT", tmp_path.resolve()),
        patch.object(vr.image_text_service, "_get_client", return_value=object()),
        patch.object(engine, "_upload_video", AsyncMock(return_value=uploaded)) as upload,
        patch.object(engine, "_delete_upload", AsyncMock()) as delete,
    ):
        with pytest.raises(RuntimeError, match="generation failed"):
            async with vr._asset_part(media) as part:
                assert part.file_data.file_uri == uploaded.uri
                raise RuntimeError("generation failed")
        upload.assert_awaited_once()
        delete.assert_awaited_once()


@async_test
async def test_storage_escape_is_rejected_before_reading_media(tmp_path):
    from services.compliance.media import transcription_service

    media = asset()
    media["storage_path"] = "../outside.png"
    with patch.object(transcription_service, "STORAGE_ROOT", tmp_path.resolve()):
        with pytest.raises(ValueError, match="unavailable in storage"):
            async with vr._asset_part(media):
                pytest.fail("An asset outside storage must never be attached")
