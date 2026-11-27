"""Contextual clearance must survive generic draft/test labels without exempting actual breaches."""
import unittest
from unittest.mock import AsyncMock, patch

from models.compliance.compliance_model import ComplianceCheckResponse
from services.compliance.evaluation import context_evaluator as ce, evidence_reconciliation as er
from services.compliance.evaluation import source_evidence_context as evidence, compliance_check_result
from test_evidence_reconciliation import finding, rule_for, source, request

HISTORY = "ZenaTech won three contracts, bringing revenue to over $500,000 across eight projects."
FORECAST = "ZenaTech expects next year's revenue to exceed $5 million."


class ContextLabelTests(unittest.TestCase):
    def candidate(self, text=HISTORY, label="test", critical=False):
        item = finding(rule="Rule 25", status="VIOLATION", text=text, category="Financial Disclosure",
                       kind="PROHIBITED_TERM", penalty=20, traffic_light="RED" if critical else "YELLOW")
        rule = rule_for(item)
        rule.severity = "CRITICAL" if critical else "HIGH"
        rule.traffic_light = "RED" if critical else "YELLOW"
        return ce.Candidate(item, rule, text, f"Caption: {label}\nText in image 1:\n{text}")

    def result(self, text=HISTORY, status="NO_VIOLATION", confidence=.95):
        return ce._Result(id="0", status=status, matched_text=text, confidence=confidence,
            reasoning="This reports completed contracts and revenue, not a forecast." if status == "NO_VIOLATION"
            else "This predicts future revenue without the required disclosure.")

    def test_supported_history_clears_under_all_labels_including_critical_rules(self):
        for label in ["test", "test only", "sample", "draft", "not for publication", ""]:
            for critical in [False, True]:
                with self.subTest(label=label, critical=critical):
                    candidate = self.candidate(label=label, critical=critical)
                    evidence.attach([candidate], [source(HISTORY)])
                    ce._apply(candidate, self.result(), "test-model", False)
                    item = candidate.finding
                    self.assertEqual((item.status, item.penalty, item.traffic_light), ("CLEARED", 0, "GREEN"))
                    self.assertEqual(item.evaluation.source, "AI")
                    self.assertEqual(item.evaluation.model_decision, "NO_VIOLATION")
                    self.assertNotIn("test label is not an exception", item.explanation)

    def test_test_label_and_published_source_do_not_exempt_real_forecasts(self):
        for label in ["test", "sample", "draft", "not for publication"]:
            candidate = self.candidate(text=FORECAST, label=label)
            checks = [source(FORECAST)]
            evidence.attach([candidate], checks)
            ce._apply(candidate, self.result(FORECAST, "VIOLATION"), "test-model", False)
            er.clear_supported([candidate.finding], checks)
            self.assertEqual((candidate.finding.status, candidate.finding.penalty), ("VIOLATION", 20))

    def test_labels_do_not_erase_evidence_requests_or_uncertainty(self):
        for status in ["NEEDS_REVIEW", "EVIDENCE_REQUIRED"]:
            candidate = self.candidate()
            ce._apply(candidate, self.result(status=status), "test-model", False)
            self.assertEqual(candidate.finding.status, status)

    def test_missing_words_low_confidence_and_injection_safeguards_remain(self):
        for critical in [False, True]:
            for changes, injected in [({"matched_text": None}, False),
                    ({"matched_text": "Invented source assertion"}, False),
                    ({"confidence": .2}, False), ({}, True)]:
                candidate = self.candidate(critical=critical)
                result = self.result().model_copy(update=changes)
                ce._apply(candidate, result, "test-model", injected)
                self.assertNotEqual(candidate.finding.status, "CLEARED")
                self.assertEqual(candidate.finding.evaluation.source, "ADJUSTED")

    def test_model_failure_never_becomes_a_clearance(self):
        for critical in [False, True]:
            candidate = self.candidate(critical=critical)
            ce._apply(candidate, None, None, False)
            self.assertNotEqual(candidate.finding.status, "CLEARED")
            self.assertEqual(candidate.finding.evaluation.source, "UNAVAILABLE")

    def test_old_cache_identity_cannot_reuse_the_previous_policy(self):
        candidate = self.candidate()
        current = ce._key(candidate)
        with patch.object(ce, "PROMPT_VERSION", "context-v13-source-evidence-financial-lookup"):
            self.assertNotEqual(current, ce._key(candidate))


class EvidencePipelineLabelTests(unittest.IsolatedAsyncioTestCase):
    async def test_source_backed_rule25_clearance_survives_full_reconciliation_and_scoring(self):
        factory = ContextLabelTests()
        for origin in ["newsroom", "source_links"]:
            with self.subTest(origin=origin):
                candidate = factory.candidate()
                item, rule = candidate.finding, candidate.rule
                checks = [source(HISTORY).model_copy(update={"where": origin})]
                req = request(candidate.context)
                req.ai_generated_images = False
                async def judge(batch, guard):
                    payload = ce._item(0, batch[0][1])
                    self.assertTrue(payload["submission_details"]["verified_sources"])
                    return {i: factory.result() for i, _ in batch}, "test-model"
                with patch.object(ce, "_judge", judge), \
                     patch.object(ce.finding_precedent_service, "similar", AsyncMock(return_value=[])), \
                     patch.object(er, "clear_supported", wraps=er.clear_supported), \
                     patch("services.compliance.evaluation.visual_review.review", AsyncMock(return_value=[])):
                    await er.run(req, [rule], [item], [(item, rule)], checks)
                response = compliance_check_result.recalculate_totals(ComplianceCheckResponse(is_compliant=False, violations=[item]))
                self.assertEqual((response.score, response.violations_count, response.cleared_count), (100, 0, 1))
                self.assertFalse(response.blocked)
                self.assertTrue(response.is_compliant)
