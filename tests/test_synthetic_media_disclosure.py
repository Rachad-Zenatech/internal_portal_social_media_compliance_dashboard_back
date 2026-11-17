import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from models.compliance.compliance_model import ComplianceCheckRequest
from services.compliance.evaluation import compliance_service as cs, synthetic_media_disclosure as disclosure
from tests.test_compliance_scoring import rule

LABEL = "Conceptual visualization - depicts a potential future use, not an actual military operation, deployment or endorsement."
POST = "Military rescue drones can help locate, assess, and support rescue operations in high-risk combat zones."


def synthetic_rule(**overrides):
    return rule(rule_number="Rule 9", title="AI Content: Never Fabricate Deceptive Photographic Proof",
                category="AI, Renders & Synthetic Media", required_disclaimers=[LABEL],
                disclaimer_triggers=["ai generated", "rendering", "concept"], **overrides)


class DisclosureTests(unittest.TestCase):
    def findings(self, text=POST, published=None, parts=None, rules=None):
        req = ComplianceCheckRequest(text=text, disclaimer_text=published,
                                     content_parts=["imagery", "caption"] if parts is None else parts)
        findings = []
        disclosure.ensure_review(req, rules or [synthetic_rule()], findings)
        return findings

    def test_military_image_without_ai_keywords_keeps_review_question(self):
        finding, = self.findings()
        self.assertEqual((finding.rule_number, finding.status, finding.kind),
                         ("Rule 9", "NEEDS_REVIEW", "MISSING_DISCLAIMER"))
        self.assertEqual(finding.penalty, 0)
        self.assertIn("cannot establish", finding.explanation)

    def test_approved_label_in_caption_or_image_clears_the_disclosure_question(self):
        self.assertEqual(self.findings(POST + " Text in image 1: " + LABEL), [])
        self.assertEqual(self.findings(published=POST + " " + LABEL), [])

    def test_unpublished_label_does_not_satisfy_the_rule(self):
        self.assertEqual(len(self.findings(POST + " " + LABEL, published=POST)), 1)

    def test_generic_illustrative_note_does_not_override_stricter_configured_label(self):
        self.assertEqual(len(self.findings(POST + " Image is for illustrative purposes only.")), 1)

    def test_generic_label_is_accepted_when_rulebook_approves_it(self):
        approved = synthetic_rule()
        approved.required_disclaimers.append("Image is for illustrative purposes only")
        self.assertEqual(self.findings(POST + " Image is for illustrative purposes only.", rules=[approved]), [])

    def test_no_image_or_nonmilitary_image_does_not_trigger(self):
        self.assertEqual(self.findings(parts=["caption"]), [])
        self.assertEqual(self.findings("Drones simplify construction inspections."), [])

    def test_existing_breach_is_not_downgraded_or_duplicated(self):
        r = synthetic_rule()
        req = ComplianceCheckRequest(text=POST + " AI generated", content_parts=["imagery"])
        finding = cs._evaluate_rule(r, " " + cs._normalize(req.text) + " ")
        findings = [finding]
        disclosure.ensure_review(req, [r], findings)
        self.assertEqual(findings, [finding])
        self.assertEqual(finding.status, "VIOLATION")

    def test_model_clearance_cannot_establish_visual_provenance(self):
        r = synthetic_rule()
        req = ComplianceCheckRequest(text=POST + " AI generated", content_parts=["imagery"])
        finding = cs._evaluate_rule(r, " " + cs._normalize(req.text) + " ")
        finding.status = "CLEARED"
        findings = [finding]
        disclosure.ensure_review(req, [r], findings)
        self.assertEqual([f.status for f in findings], ["NEEDS_REVIEW"])


class RerunTests(unittest.IsolatedAsyncioTestCase):
    async def test_empty_semantic_reruns_always_keep_rule_nine(self):
        req = ComplianceCheckRequest(text=POST, content_parts=["imagery", "caption"])
        with patch.object(cs, "applicable_rules", AsyncMock(return_value=[synthetic_rule()])), \
             patch.object(cs, "rulebook_version", AsyncMock(return_value="test-rules")), \
             patch.object(cs, "_semantic_findings", AsyncMock(return_value=[])), \
             patch.object(cs.context_evaluator, "evaluate", AsyncMock()), \
             patch.object(cs.source_check_service, "run", AsyncMock(return_value=SimpleNamespace(ai_failed=False, checks=[]))):
            for _ in range(2):
                result = await cs.check_compliance(req)
                finding, = [f for f in result.violations if f.rule_number == "Rule 9"]
                self.assertEqual(finding.status, "NEEDS_REVIEW")
                self.assertEqual(result.traffic_light, "YELLOW")
                self.assertEqual(finding.penalty, 5)
                self.assertEqual(result.score, 95)
                self.assertFalse(result.blocked)
