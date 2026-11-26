from types import SimpleNamespace

import pytest

from services.compliance.evaluation.finding_visibility import is_visible
from services.compliance.evaluation.compliance_check_result import recalculate_totals


def finding(confidence, status="NEEDS_REVIEW", number="Rule 18", source="AI"):
    return SimpleNamespace(rule_number=number, status=status, penalty=5, traffic_light="YELLOW",
                           evaluation=SimpleNamespace(confidence=confidence, source=source))


@pytest.mark.parametrize("number", ["Rule 18", "Rule 25"])
@pytest.mark.parametrize("status", ["VIOLATION", "NEEDS_REVIEW", "EVIDENCE_REQUIRED"])
@pytest.mark.parametrize("confidence,visible", [(0.5, False), (0.699, False), (0.7, True), (0.85, True)])
def test_rule_cutoff_is_inclusive(confidence, visible, status, number):
    assert is_visible(finding(confidence, status, number)) is visible


def test_other_rules_cleared_findings_and_rule_based_evidence_keep_their_policy():
    assert is_visible(finding(0.6, number="Rule 29"))
    assert is_visible(finding(0.2, status="CLEARED"))
    rule_based = finding(0)
    rule_based.evaluation = None
    assert is_visible(rule_based)
    assert is_visible(finding(0, source="UNAVAILABLE"))


@pytest.mark.parametrize("number", ["Rule 18", "Rule 25"])
def test_hidden_findings_do_not_reduce_score_or_counts(number):
    result = SimpleNamespace(violations=[finding(0.6, number=number), finding(0.7, number=number)], ai_unavailable=False, score_cap=None)
    recalculate_totals(result)
    assert len(result.violations) == 1
    assert result.review_count == 1
    assert result.score == 95
    assert result.blocked is False


def test_rule25_requires_an_actual_ai_confidence_and_preserves_cleared_history():
    missing = finding(0, number="Rule 25")
    missing.evaluation = None
    assert not is_visible(missing)
    assert not is_visible(finding(1, number="Rule 25", source="UNAVAILABLE"))
    assert not is_visible(finding(0.699, number="Rule 25", source="ADJUSTED"))
    assert is_visible(finding(0.7, number="Rule 25", source="ADJUSTED"))
    assert is_visible(finding(0.2, status="CLEARED", number="Rule 25"))
