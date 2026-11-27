import asyncio
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

from models.compliance.compliance_model import ComplianceCheckRequest, ComplianceRule, ContextEvaluation
from services.compliance.evaluation import compliance_service as engine, rule18_policy, script_compliance_check, context_candidates
from services.compliance.media.script_content import VERSION, sections, published
from test_script_scoring import finding


@pytest.fixture
def rules(monkeypatch):
    rule = ComplianceRule(id=uuid4(), rule_number="Rule 1", category="Tests", title="Restricted wording",
                          summary="Do not use restrictedword", prohibited_terms=["restrictedword"],
                          severity="CRITICAL", traffic_light="RED", created_at=datetime.now(timezone.utc),
                          updated_at=datetime.now(timezone.utc))
    monkeypatch.setattr(engine, "applicable_rules", AsyncMock(return_value=[rule]))
    monkeypatch.setattr(engine, "_semantic_findings", AsyncMock(return_value=[]))
    monkeypatch.setattr(engine.context_evaluator, "evaluate", AsyncMock())
    monkeypatch.setattr(engine.visual_review, "review", AsyncMock(return_value=[]))
    monkeypatch.setattr(context_candidates, "for_post", lambda *args: [])
    monkeypatch.setattr(engine, "rulebook_version", AsyncMock(return_value="test-rules"))
    monkeypatch.setattr(rule18_policy, "applies", lambda req: True)
    monkeypatch.setattr(rule18_policy, "check_sources", AsyncMock(return_value=SimpleNamespace(
        checks=[], unbacked=[], failed_claims=[], ai_failed=False, audit=[])))
    return rule


def request(visual="Safe camera direction", voice="Safe spoken sentence", supers="SAFE", title="Safe title"):
    text = f"{title}\n| Time | Visuals | Voiceover | Supers |\n| 0:00 | {visual} | {voice} | {supers} |"
    req = ComplianceCheckRequest(text=text, content_type="script")
    req._script_sections = sections(text, title)
    req.disclaimer_text = published(req._script_sections)
    return req


@pytest.mark.parametrize("field", ["visual", "title"])
def test_production_flags_cannot_reduce_or_block_the_score(rules, field):
    result = asyncio.run(engine.check_compliance(request(**{field: "restrictedword"})))
    assert result.score == result.raw_score == 100
    assert not result.blocked
    assert result.violations_count == 0
    assert result.review_count == 1
    assert result.violations[0].status == "NEEDS_REVIEW"
    assert result.violations[0].penalty == 0


@pytest.mark.parametrize("field", ["voice", "supers"])
def test_published_copy_keeps_normal_critical_penalties(rules, field):
    result = asyncio.run(engine.check_compliance(request(**{field: "restrictedword"})))
    assert result.score == 65
    assert result.blocked
    assert result.violations_count == 1


def test_a_visual_occurrence_does_not_hide_the_same_claim_in_voiceover(rules):
    result = asyncio.run(engine.check_compliance(request(visual="restrictedword", voice="restrictedword")))
    assert result.score == 65
    assert result.violations_count == result.review_count == 1
    assert sorted(item.penalty for item in result.violations) == [0, 35]


def test_extra_production_claims_do_not_increase_published_penalties(rules):
    req = request(visual="restrictedword. restrictedword. restrictedword.", voice="restrictedword")
    result = asyncio.run(engine.check_compliance(req))
    assert result.score == 65
    assert all(item.extra_claims == 0 for item in result.violations)


def test_normal_social_post_scoring_is_unchanged(rules):
    result = asyncio.run(engine.check_compliance(ComplianceCheckRequest(text="restrictedword", content_type="social_post")))
    assert result.score == 65 and result.blocked
    assert result.review_count == 0
    assert not result.violations[0].script_review_only


def test_a_rule_implied_only_by_the_unpublished_title_cannot_deduct(rules, monkeypatch):
    async def applicable(req):
        return [rules] if "INTERNAL_CONTEXT" in req.text else []
    monkeypatch.setattr(engine, "applicable_rules", applicable)
    result = asyncio.run(engine.check_compliance(request(title="INTERNAL_CONTEXT restrictedword", voice="restrictedword")))
    assert result.score == 100
    assert result.review_count == 1
    assert all(item.script_review_only for item in result.violations)


def test_source_findings_for_published_claims_still_cost_points(rules, monkeypatch):
    item = finding("Published factual claim", category="Source Check", status="EVIDENCE_REQUIRED", penalty=0)
    monkeypatch.setattr(rule18_policy, "check_sources", AsyncMock(return_value=SimpleNamespace(
        checks=[], unbacked=[item], failed_claims=[], ai_failed=False, audit=[])))
    result = asyncio.run(engine.check_compliance(request(voice="Published factual claim")))
    assert result.score < 100
    assert result.evidence_count == 1
    assert not result.violations[0].script_review_only


def test_a_not_found_source_finding_in_production_notes_cannot_be_charged(rules, monkeypatch):
    item = finding("Internal factual claim", category="Source Check", status="NEEDS_REVIEW",
                   title=engine.source_check_service.NOT_FOUND_TITLE)
    monkeypatch.setattr(rule18_policy, "check_sources", AsyncMock(return_value=SimpleNamespace(
        checks=[], unbacked=[item], failed_claims=[], ai_failed=False, audit=[])))
    result = asyncio.run(engine.check_compliance(request(visual="Internal factual claim")))
    assert result.score == 100
    assert result.review_count == 1 and result.evidence_count == 0
    assert result.violations[0].penalty == result.violations[0].uncounted_penalty == 0


def test_an_unpublished_disclaimer_does_not_clear_a_published_claim(rules):
    rules.required_disclaimers = ["Required disclosure"]
    rules.disclaimer_triggers = ["restrictedword"]
    req = request(voice="restrictedword", visual="Required disclosure", title="Required disclosure")
    result = asyncio.run(engine.check_compliance(req))
    assert any(item.missing_disclaimers and not item.script_review_only for item in result.violations)
    assert result.score < 100


def test_high_confidence_production_review_is_still_free(rules, monkeypatch):
    item = finding("Camera requires approval", status="NEEDS_REVIEW", rule_id=rules.id,
                   evaluation=ContextEvaluation(decision="NEEDS_REVIEW", confidence=1, reasoning="Certain"))
    monkeypatch.setattr(engine, "_semantic_findings", AsyncMock(return_value=[item]))
    result = asyncio.run(engine.check_compliance(request(visual="Camera requires approval")))
    assert result.score == 100
    assert result.review_count == 1


def test_source_reader_receives_only_published_copy_and_keeps_script_sources(monkeypatch):
    reader = AsyncMock(return_value=SimpleNamespace())
    monkeypatch.setattr(rule18_policy.source_check_service, "run", reader)
    req = request(visual="Internal factual instruction", voice="Published claim", supers="PUBLIC CLAIM")
    req.source_script = "Sources:\nhttps://example.com/evidence"
    asyncio.run(rule18_policy.check_sources(req))
    checked_text = reader.await_args.args[0]
    assert "Published claim" in checked_text and "PUBLIC CLAIM" in checked_text
    assert "Internal factual instruction" not in checked_text and "Safe title" not in checked_text
    assert reader.await_args.kwargs["source_script"] == req.source_script


def test_script_wrapper_keeps_all_text_in_the_rule_check(monkeypatch):
    captured = []
    async def check(req):
        captured.append(req)
        return engine.ComplianceCheckResponse(is_compliant=True)
    monkeypatch.setattr(script_compliance_check, "check_compliance", check)
    monkeypatch.setattr(script_compliance_check.subs, "check_request", lambda row, body, **kwargs:
                        ComplianceCheckRequest(text=body, source_script=kwargs["source_script"]))
    monkeypatch.setattr(script_compliance_check.check_snapshot_service, "inputs", lambda *args, **kwargs: ({}, {}))
    async def save(post, evaluated, rules, check):
        return check
    monkeypatch.setattr(script_compliance_check.check_snapshot_service, "save", save)
    text = "Storyline: Internal angle\n| Time | Visuals | Voiceover | Supers |\n| 0:00 | Internal camera direction | Spoken claim | PUBLIC TEXT |"
    result = asyncio.run(script_compliance_check.check_text({"title": "Title"}, text))
    assert "Internal camera direction" in captured[0].text
    assert "Internal angle" in captured[0].text
    assert captured[0].disclaimer_text == "Spoken claim\nPUBLIC TEXT"
    assert result["script_scoring_version"] == VERSION


def test_new_rule_evidence_lookup_receives_no_production_claims(monkeypatch):
    from services.compliance.evaluation import source_evidence_lookup, script_scoring
    lookup = AsyncMock(return_value=(SimpleNamespace(), []))
    monkeypatch.setattr(source_evidence_lookup, "check", lookup)
    req = request(visual="Internal factual claim", voice="Published factual claim")
    internal, public = finding("Internal factual claim"), finding("Published factual claim")
    asyncio.run(script_scoring.verify_script_sources(req, [internal, public], []))
    checked, candidates, _ = lookup.await_args.args
    assert checked.text == "Published factual claim\nSAFE"
    assert candidates == [public]


def test_new_evidence_lookup_preserves_the_normal_post_request(monkeypatch):
    from services.compliance.evaluation import source_evidence_lookup, script_scoring
    lookup = AsyncMock(return_value=(SimpleNamespace(), []))
    monkeypatch.setattr(source_evidence_lookup, "check", lookup)
    req = ComplianceCheckRequest(text="A normal social post")
    candidates = [finding("normal")]
    asyncio.run(script_scoring.verify_script_sources(req, candidates, []))
    assert lookup.await_args.args[0] is req
    assert lookup.await_args.args[1] is candidates
