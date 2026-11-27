from uuid import uuid4

import pytest

from models.compliance.compliance_model import ComplianceCheckRequest, ComplianceViolation, ContextEvaluation
from services.compliance.evaluation.script_scoring import apply_policy
from services.compliance.media.script_content import sections


def finding(term="combat proven", **changes):
    values = dict(rule_id=uuid4(), rule_number="Rule 4", title="Example", category="Defense",
                  severity="CRITICAL", traffic_light="RED", flagged_terms=[term], explanation="Review this wording.",
                  penalty=35, uncounted_penalty=35, extra_claims=2, counted_with="Rule 3")
    return ComplianceViolation(**(values | changes))


def script(visual="combat proven", voice="A safe sentence", supers="SAFE"):
    return sections(f"| Time | Visuals | Voiceover | Supers |\n| 0:00 | {visual} | {voice} | {supers} |")


@pytest.mark.parametrize("status", ["VIOLATION", "EVIDENCE_REQUIRED", "NEEDS_REVIEW"])
def test_visual_findings_become_review_without_any_restorable_deductions(status):
    item = finding(status=status, evaluation=ContextEvaluation(decision=status, confidence=1, reasoning="Certain"))
    apply_policy([item], script())
    assert item.status == "NEEDS_REVIEW"
    assert item.traffic_light == "YELLOW"
    assert item.penalty == item.uncounted_penalty == item.extra_claims == 0
    assert item.counted_with is None
    assert item.script_review_only
    assert item.script_sections == ["visuals"]


@pytest.mark.parametrize("column", ["voiceover", "supers"])
def test_published_findings_keep_their_status_and_deductions(column):
    item = finding()
    parts = script(visual="Camera", voice="combat proven" if column == "voiceover" else "A sentence",
                   supers="combat proven" if column == "supers" else "SAFE")
    apply_policy([item], parts)
    assert item.status == "VIOLATION"
    assert item.penalty == 35
    assert not item.script_review_only
    assert item.script_sections == [column]


def test_the_judged_quote_takes_priority_over_a_trigger_repeated_in_other_columns():
    item = finding(passage="Show a combat proven aircraft in stock footage.")
    parts = script(visual=item.passage, voice="Avoid saying combat proven without evidence.")
    apply_policy([item], parts)
    assert item.script_review_only
    assert item.penalty == 0


def test_distinct_triggers_from_the_same_rule_are_separated_by_scope():
    items = [finding(flagged_terms=["battle tested", "combat proven"])]
    apply_policy(items, script(visual="battle tested", voice="combat proven"))
    assert len(items) == 2
    assert items[0].flagged_terms == ["combat proven"]
    assert items[0].penalty == 35
    assert items[1].flagged_terms == ["battle tested"]
    assert items[1].script_review_only and items[1].penalty == 0


def test_unknown_format_and_title_findings_stay_review_only():
    item = finding()
    apply_policy([item], sections("Unlabelled script", "combat proven"))
    assert item.status == "NEEDS_REVIEW" and item.penalty == 0


def test_public_requests_cannot_enable_a_script_scoring_exemption():
    request = ComplianceCheckRequest(text="social post", _script_sections={"details": "social post"})
    assert request._script_sections is None


def test_missing_disclaimer_still_applies_to_identified_published_copy():
    item = finding(kind="MISSING_DISCLAIMER", flagged_terms=[], missing_disclaimers=["Required disclosure"])
    apply_policy([item], script())
    assert not item.script_review_only
    assert item.penalty == 35
