import unittest
from types import SimpleNamespace
from uuid import uuid4

from models.compliance.compliance_model import ComplianceViolation, ContextEvaluation
from services.compliance.analytics.post_signals import PostSignals
from services.compliance.evaluation.compliance_check_result import build_response


class FindingVisibilityTests(unittest.TestCase):
    def finding(self, confidence, status="NEEDS_REVIEW", source="AI"):
        return ComplianceViolation(
            rule_id=uuid4(), rule_number="Rule 4", title="Test", category="Test",
            severity="HIGH", traffic_light="YELLOW", flagged_terms=["Army"],
            explanation="Test", status=status,
            evaluation=ContextEvaluation(decision=status, confidence=confidence, source=source),
        )

    def response(self, findings):
        scope = SimpleNamespace(audiences=[], content_parts=[], added_rule_sets={}, implied_audiences=[])
        return build_response(scope, [], findings, [], [], [], PostSignals(), False, [], [], "test")

    def test_low_confidence_review_is_omitted_from_findings_counts_and_recommendations(self):
        result = self.response([self.finding(0.49)])
        self.assertEqual(result.violations, [])
        self.assertEqual(result.review_count, 0)
        self.assertEqual(result.score, 100)
        self.assertEqual(result.traffic_light, "GREEN")
        self.assertTrue(result.is_compliant)
        self.assertFalse(any("Confirm them" in note for note in result.recommendations))

    def test_threshold_is_inclusive_and_confidence_is_preserved(self):
        finding = self.finding(0.5)
        result = self.response([self.finding(0.1), finding])
        self.assertEqual(result.violations, [finding])
        self.assertEqual(result.review_count, 1)
        self.assertEqual(result.traffic_light, "YELLOW")
        self.assertEqual(result.model_dump()["violations"][0]["evaluation"]["confidence"], 0.5)

    def test_adjusted_low_confidence_review_is_omitted(self):
        self.assertEqual(self.response([self.finding(0.4, source="ADJUSTED")]).violations, [])

    def test_unavailable_and_rule_based_reviews_remain_visible(self):
        unavailable = self.finding(0, source="UNAVAILABLE")
        rule_based = self.finding(0)
        rule_based.evaluation = None
        result = self.response([unavailable, rule_based])
        self.assertEqual(result.violations, [unavailable, rule_based])
        self.assertEqual(result.review_count, 2)

    def test_other_statuses_are_preserved(self):
        findings = [self.finding(0.2, status=status) for status in
                    ("VIOLATION", "EVIDENCE_REQUIRED", "CLEARED")]
        self.assertEqual(self.response(findings).violations, findings)


if __name__ == "__main__":
    unittest.main()
