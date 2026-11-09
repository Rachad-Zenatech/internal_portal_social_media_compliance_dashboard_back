import unittest
from types import SimpleNamespace
from uuid import uuid4

from models.compliance.compliance_model import ComplianceViolation, ContextEvaluation
from services.compliance.evaluation import compliance_service
from services.compliance.submissions.flag_review_service import score_after_dismissals


def rule(severity="CRITICAL", light="RED"):
    return SimpleNamespace(id=uuid4(), severity=severity, traffic_light=light)


def to_check(r, confidence, source="AI", kind="PROHIBITED_TERM", status="NEEDS_REVIEW"):
    return ComplianceViolation(
        rule_id=r.id, rule_number="Rule 4", title="t", category="c", severity=r.severity, traffic_light="YELLOW",
        flagged_terms=[], explanation="", status=status, kind=kind,
        evaluation=ContextEvaluation(decision=status, confidence=confidence, source=source),
    )


def charge(r, v):
    compliance_service._charge_review_findings([v], [r])
    return v.penalty


class ReviewPenaltyTests(unittest.TestCase):
    def test_sure_costs_half_the_violation_points(self):
        self.assertEqual(charge(red := rule(), to_check(red, 0.9)), 18)  # half of 35, rounded up
        self.assertEqual(charge(high := rule("HIGH", "YELLOW"), to_check(high, 0.8)), 10)
        self.assertEqual(charge(med := rule("MEDIUM", "GREEN"), to_check(med, 0.95)), 5)

    def test_a_missing_disclaimer_costs_half_of_its_rules_points(self):
        red, high = rule(), rule("HIGH", "YELLOW")
        self.assertEqual(charge(red, to_check(red, 0.9, kind="MISSING_DISCLAIMER")), 18)  # Rule 4: half of 35
        self.assertEqual(charge(high, to_check(high, 1.0, kind="MISSING_DISCLAIMER")), 10)  # Rule 25: half of 20

    def test_fairly_sure_costs_five(self):
        r = rule()
        self.assertEqual(charge(r, to_check(r, 0.5)), 5)
        self.assertEqual(charge(r, to_check(r, 0.79)), 5)

    def test_unsure_costs_nothing(self):
        r = rule()
        self.assertEqual(charge(r, to_check(r, 0.49)), 0)

    def test_only_the_models_own_judgment_counts(self):
        r = rule()
        # ADJUSTED: the model would have cleared it; UNAVAILABLE: no model read it.
        self.assertEqual(charge(r, to_check(r, 0.95, source="ADJUSTED")), 0)
        self.assertEqual(charge(r, to_check(r, 0.0, source="UNAVAILABLE")), 0)

    def test_other_statuses_are_left_alone(self):
        r = rule()
        self.assertEqual(charge(r, to_check(r, 0.95, status="CLEARED")), 0)

    def test_it_is_a_warning_so_no_critical_cap_and_dismissing_gives_points_back(self):
        r = rule()
        v = to_check(r, 0.9)
        compliance_service._charge_review_findings([v], [r])
        check = {"score": 82, "violations": [v.model_dump(mode="json")]}
        self.assertEqual(score_after_dismissals(check, set()), 82)
        self.assertEqual(score_after_dismissals(check, {str(r.id)}), 100)


if __name__ == "__main__":
    unittest.main()
