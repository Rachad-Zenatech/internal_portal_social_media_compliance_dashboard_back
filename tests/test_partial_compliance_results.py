"""A failed later stage must preserve completed findings without enabling approval."""
from types import SimpleNamespace
from uuid import uuid4

from models.compliance.compliance_model import ComplianceViolation
from services.compliance.evaluation.compliance_check_result import build_response


def test_incomplete_review_preserves_critical_finding_and_penalty():
    finding = ComplianceViolation(rule_id=uuid4(), rule_number="Rule 7", title="Sensitive information",
        category="Test", severity="CRITICAL", traffic_light="RED", kind="PROHIBITED_TERM",
        explanation="Confirmed sensitive content", flagged_terms=["secret"], penalty=35, status="VIOLATION")
    scope = SimpleNamespace(audiences=[], content_parts=[], added_rule_sets={}, implied_audiences=[])
    signals = SimpleNamespace(cashtags=[], hashtags=[], mentions=[], links=[], government_references=[])
    result = build_response(scope, [], [finding], [finding], [], [], signals, True, [], [], "test")
    assert result.ai_unavailable and not result.is_compliant
    assert result.blocked and result.traffic_light == "RED"
    assert result.violations == [finding]
    assert result.score == 65 and result.raw_score == 65
