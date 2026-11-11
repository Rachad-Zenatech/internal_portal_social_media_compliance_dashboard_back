import unittest
from uuid import uuid4

from models.compliance.compliance_model import ComplianceViolation, ContextEvaluation
from services.compliance.evaluation import compliance_service
from services.compliance.submissions.flag_review_service import score_after_dismissals

CLAIM = "Reclaim up to 3,000 hours of manual compliance work"


def finding(number, severity="MEDIUM", said=CLAIM, status="EVIDENCE_REQUIRED"):
    return ComplianceViolation(
        rule_id=uuid4(), rule_number=number, title="t", category="c", severity=severity, traffic_light="YELLOW",
        flagged_terms=[], explanation="", status=status,
        evaluation=ContextEvaluation(decision=status, matched_text=said),
    )


class UnsupportedClaimTests(unittest.TestCase):
    def test_a_claim_that_needs_a_source_costs_five_points(self):
        a = finding("Rule 18")
        compliance_service._charge_unsupported_claims([a])
        self.assertEqual(a.penalty, compliance_service.EVIDENCE_PENALTY)

    def test_the_same_words_under_two_rules_cost_once_at_the_stricter_rule(self):
        medium, high = finding("Rule 18", "MEDIUM"), finding("Rule 24", "HIGH")
        costing = compliance_service._charge_unsupported_claims([medium, high])
        self.assertEqual((high.penalty, medium.penalty, medium.counted_with), (5, 0, "Rule 24"))
        self.assertEqual(costing, [high])

    def test_the_total_is_capped_at_ten(self):
        claims = [finding(f"Rule {i}", said=f"Claim number {i}") for i in range(5)]
        compliance_service._charge_unsupported_claims(claims)
        self.assertEqual(sum(c.penalty for c in claims), compliance_service.EVIDENCE_CAP)

    def test_checks_for_a_person_cost_nothing(self):
        review = finding("Rule 9", status="NEEDS_REVIEW")
        compliance_service._charge_unsupported_claims([review])
        self.assertEqual(review.penalty, 0)

    def test_dismissing_the_flag_gives_the_points_back(self):
        a = finding("Rule 18")
        compliance_service._charge_unsupported_claims([a])
        check = {"score": 95, "violations": [a.model_dump(mode="json")]}
        self.assertEqual(score_after_dismissals(check, set()), 95)
        self.assertEqual(score_after_dismissals(check, {str(a.rule_id)}), 100)

    def test_dismissing_the_stricter_flag_makes_the_other_rule_carry_the_claim(self):
        medium, high = finding("Rule 18", "MEDIUM"), finding("Rule 24", "HIGH")
        compliance_service._charge_unsupported_claims([medium, high])
        check = {"score": 95, "violations": [v.model_dump(mode="json") for v in (medium, high)]}
        self.assertEqual(score_after_dismissals(check, {str(high.rule_id)}), 95)


if __name__ == "__main__":
    unittest.main()
