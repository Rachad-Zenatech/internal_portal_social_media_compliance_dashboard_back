import unittest
from unittest.mock import AsyncMock, patch

from models.compliance.compliance_model import ComplianceCheckRequest
from services.compliance.evaluation import compliance_service as cs, synthetic_media_disclosure as disclosure
from tests.test_compliance_scoring import rule

LABEL = "Conceptual visualization - depicts a potential future use, not an actual military operation, deployment or endorsement."
POST = "Military rescue drones can help locate, assess, and support rescue operations in high-risk combat zones."
AI_POST = POST + " This post uses an AI-generated image."


def synthetic_rule(**overrides):
    return rule(rule_number="Rule 9", title="AI Content: Never Fabricate Deceptive Photographic Proof",
                category="AI, Renders & Synthetic Media", required_disclaimers=[LABEL],
                disclaimer_triggers=["ai generated", "rendering", "concept"], **overrides)


class DisclosureTests(unittest.TestCase):
    def findings(self, text=AI_POST, published=None, parts=None, rules=None):
        req = ComplianceCheckRequest(text=text, disclaimer_text=published,
                                     content_parts=["imagery", "caption"] if parts is None else parts)
        findings = []
        disclosure.ensure_review(req, rules or [synthetic_rule()], findings)
        return findings

    def test_military_topic_does_not_imply_generated_photographs(self):
        self.assertEqual(self.findings(POST), [])
        self.assertEqual(self.findings("Event photographs from the Drone & AAM Policy Symposium. " + POST), [])
        self.assertEqual(self.findings(POST + " #AIPoweredDrones"), [])

    def test_declared_generated_imagery_keeps_review_question(self):
        finding, = self.findings()
        self.assertEqual((finding.rule_number, finding.status, finding.kind),
                         ("Rule 9", "NEEDS_REVIEW", "MISSING_DISCLAIMER"))
        self.assertEqual(finding.penalty, 0)
        self.assertIn("AI-generated image", finding.flagged_terms[0])

    def test_ai_product_capabilities_and_negated_generation_do_not_trigger(self):
        for text in (POST + " AI-powered drones use image recognition.",
                     POST + " AI image recognition supports drone navigation.",
                     POST + " No AI-generated images were used.",
                     POST + " This is not an AI-generated image.",
                     POST + " The caption was AI generated. The images are event photographs."):
            self.assertEqual(self.findings(text), [])

    def test_generated_visual_descriptions_are_recognised(self):
        for text in ("AI-generated military image", "synthetic combat footage", "conceptual visualization",
                     "a 3d render of a military operation", "the image was AI-generated"):
            self.assertEqual(len(self.findings(POST + " " + text)), 1)


    def test_approved_label_in_caption_or_image_clears_the_disclosure_question(self):
        self.assertEqual(self.findings(AI_POST + " Text in image 1: " + LABEL), [])
        self.assertEqual(self.findings(published=AI_POST + " " + LABEL), [])

    def test_unpublished_label_does_not_satisfy_the_rule(self):
        self.assertEqual(len(self.findings(AI_POST + " " + LABEL, published=AI_POST)), 1)

    def test_generic_illustrative_note_does_not_override_stricter_configured_label(self):
        self.assertEqual(len(self.findings(AI_POST + " Image is for illustrative purposes only.")), 1)

    def test_generic_label_is_accepted_when_rulebook_approves_it(self):
        approved = synthetic_rule()
        approved.required_disclaimers.append("Image is for illustrative purposes only")
        self.assertEqual(self.findings(AI_POST + " Image is for illustrative purposes only.", rules=[approved]), [])

    def test_no_image_or_nonmilitary_image_does_not_trigger(self):
        self.assertEqual(self.findings(parts=["caption"]), [])
        self.assertEqual(self.findings("Drones simplify construction inspections."), [])

    def test_creator_declaration_requires_label_for_nonmilitary_images(self):
        req = ComplianceCheckRequest(text="Construction site illustration", content_parts=["imagery"],
                                     ai_generated_images=True)
        rule = synthetic_rule()
        findings = []
        disclosure.ensure_review(req, [rule], findings)
        self.assertEqual([(f.rule_number, f.kind) for f in findings], [("Rule 9", "MISSING_DISCLAIMER")])
        findings = []
        disclosure.ensure_review(req.model_copy(update={"disclaimer_text": LABEL}), [rule], findings)
        self.assertEqual(findings, [])

    def test_existing_breach_is_not_downgraded_or_duplicated(self):
        r = synthetic_rule()
        req = ComplianceCheckRequest(text=AI_POST, content_parts=["imagery"])
        finding = cs._evaluate_rule(r, " " + cs._normalize(req.text) + " ")
        findings = [finding]
        disclosure.ensure_review(req, [r], findings)
        self.assertEqual(findings, [finding])
        self.assertEqual(finding.status, "VIOLATION")

    def test_model_clearance_cannot_establish_visual_provenance(self):
        r = synthetic_rule()
        req = ComplianceCheckRequest(text=AI_POST, content_parts=["imagery"])
        finding = cs._evaluate_rule(r, " " + cs._normalize(req.text) + " ")
        finding.status = "CLEARED"
        findings = [finding]
        disclosure.ensure_review(req, [r], findings)
        self.assertEqual([f.status for f in findings], ["NEEDS_REVIEW"])


class PublishedDisclosureTests(unittest.TestCase):
    def test_sub_1186_extracted_note_matches_approved_illustrative_label(self):
        r = synthetic_rule()
        r.required_disclaimers.append("Image for illustrative purposes only")
        for note in ("*Image is for illustration purposes only.", "Image is for illustrative purposes only.",
                     "Images are for illustration purposes only."):
            with self.subTest(note=note):
                text = "ZENATECH DRONE AS A SERVICE NOVEMBER 10, 2025 NEWS TODAY! " + note
                req = ComplianceCheckRequest(text=text, disclaimer_text=text,
                                             content_parts=["imagery", "caption"], ai_generated_images=True)
                finding = cs._evaluate_rule(r, " " + cs._normalize(AI_POST) + " ")
                finding.status, finding.penalty = "NEEDS_REVIEW", 5
                disclosure.ensure_review(req, [r], [finding])
                self.assertEqual((finding.status, finding.penalty), ("CLEARED", 0))

    def test_illustrative_note_still_must_be_approved_and_not_negated(self):
        for labels, note in (([LABEL], "Image is for illustration purposes only."),
                             (["Image for illustrative purposes only"], "Image is not for illustration purposes only.")):
            with self.subTest(labels=labels, note=note):
                r = synthetic_rule()
                r.required_disclaimers = labels
                req = ComplianceCheckRequest(text=note, content_parts=["imagery"], ai_generated_images=True)
                findings = []
                disclosure.ensure_review(req, [r], findings)
                self.assertEqual([f.kind for f in findings], ["MISSING_DISCLAIMER"])

    def test_approved_image_label_clears_existing_missing_disclaimer(self):
        r = synthetic_rule()
        alternate = "Simulation or rendering - not test footage."
        r.required_disclaimers.append(alternate)
        for label in (LABEL, LABEL.replace("a potential", "potential"), alternate):
            with self.subTest(label=label):
                req = ComplianceCheckRequest(text=POST + "\nText in image 1: " + label,
                    disclaimer_text=POST + "\nText in image 1: " + label,
                    content_parts=["imagery", "caption"], ai_generated_images=True)
                finding = cs._evaluate_rule(r, " " + cs._normalize(AI_POST) + " ")
                finding.status, finding.penalty = "NEEDS_REVIEW", 5
                disclosure.ensure_review(req, [r], [finding])
                self.assertEqual((finding.status, finding.penalty, finding.traffic_light), ("CLEARED", 0, "GREEN"))
                self.assertEqual(finding.missing_disclaimers, [])
                self.assertEqual(finding.evaluation.decision, "CLEARED")

    def test_generic_ai_disclosure_does_not_override_required_labels(self):
        r = synthetic_rule()
        for label in ("AI-generated image", "This image was generated using AI.", "Image for illustrative purposes only."):
            with self.subTest(label=label):
                req = ComplianceCheckRequest(text=POST + " " + label, disclaimer_text=label,
                                             content_parts=["imagery"], ai_generated_images=True)
                findings = []
                disclosure.ensure_review(req, [r], findings)
                self.assertEqual([f.kind for f in findings], ["MISSING_DISCLAIMER"])

    def test_approved_label_does_not_clear_deceptive_claim(self):
        r = synthetic_rule()
        r.prohibited_terms = ["fabricated telemetry"]
        req = ComplianceCheckRequest(text="AI-generated image with fabricated telemetry. " + LABEL,
                                     content_parts=["imagery"], ai_generated_images=True)
        finding = cs._evaluate_rule(r, " " + cs._normalize(req.text) + " ")
        disclosure.ensure_review(req, [r], [finding])
        self.assertEqual(finding.status, "VIOLATION")
        self.assertGreater(finding.penalty, 0)


class RerunTests(unittest.IsolatedAsyncioTestCase):
    async def test_approved_image_label_clears_stale_finding_without_deducting_points(self):
        req = ComplianceCheckRequest(text=POST + "\nText in image 1: " + LABEL,
                                     disclaimer_text=POST + "\nText in image 1: " + LABEL,
                                     content_parts=["imagery", "caption"], ai_generated_images=True)
        r = synthetic_rule()
        stale_finding = cs._evaluate_rule(r, " " + cs._normalize(AI_POST) + " ")
        stale_finding.status = "NEEDS_REVIEW"
        with patch.object(cs, "applicable_rules", AsyncMock(return_value=[r])), \
             patch.object(cs, "rulebook_version", AsyncMock(return_value="test-rules")), \
             patch.object(cs, "_semantic_findings", AsyncMock(return_value=[stale_finding])), \
             patch.object(cs.context_evaluator, "evaluate", AsyncMock()), \
             patch.object(cs.source_check_service, "run", AsyncMock(return_value=cs.source_check_service.SourceCheckResult())):
            for _ in range(2):
                result = await cs.check_compliance(req)
                self.assertEqual(result.score, 100)
                self.assertFalse(result.blocked)
                self.assertEqual([(f.status, f.penalty) for f in result.violations if f.rule_id == r.id], [("CLEARED", 0)])

    async def test_declared_synthetic_media_reruns_keep_rule_nine(self):
        req = ComplianceCheckRequest(text=POST + " Synthetic military image.", content_parts=["imagery", "caption"])
        with patch.object(cs, "applicable_rules", AsyncMock(return_value=[synthetic_rule()])), \
             patch.object(cs, "rulebook_version", AsyncMock(return_value="test-rules")), \
             patch.object(cs, "_semantic_findings", AsyncMock(return_value=[])), \
             patch.object(cs.context_evaluator, "evaluate", AsyncMock()), \
             patch.object(cs.source_check_service, "run", AsyncMock(return_value=cs.source_check_service.SourceCheckResult())):
            for _ in range(2):
                result = await cs.check_compliance(req)
                finding, = [f for f in result.violations if f.rule_number == "Rule 9"]
                self.assertEqual(finding.status, "NEEDS_REVIEW")
                self.assertEqual(result.traffic_light, "YELLOW")
                self.assertEqual(finding.penalty, 5)
                self.assertEqual(result.score, 95)
                self.assertFalse(result.blocked)


class UnsupportedFindings(unittest.TestCase):
    def test_semantic_topic_only_candidate_is_removed_before_scoring(self):
        r = synthetic_rule()
        req = ComplianceCheckRequest(text=POST, content_parts=["imagery", "caption"])
        for kind in ("MISSING_DISCLAIMER", "PROHIBITED_TERM"):
            candidate = cs.ComplianceViolation(rule_id=r.id, rule_number=r.rule_number, title=r.title,
                category=r.category, severity=r.severity, traffic_light="YELLOW", status="NEEDS_REVIEW",
                kind=kind, flagged_terms=["military"], explanation="Military topic, no generation evidence", penalty=5)
            findings = [candidate]
            disclosure.ensure_review(req, [r], findings)
            self.assertEqual(findings, [])

    def test_actual_prohibited_deception_is_preserved(self):
        r = synthetic_rule()
        r.prohibited_terms = ["fabricated telemetry"]
        req = ComplianceCheckRequest(text="Military demonstration with fabricated telemetry", content_parts=["imagery"])
        finding = cs._evaluate_rule(r, " " + cs._normalize(req.text) + " ")
        findings = [finding]
        disclosure.ensure_review(req, [r], findings)
        self.assertEqual(findings, [finding])

    def test_other_rules_are_unchanged(self):
        r = synthetic_rule()
        other = rule(rule_number="Rule 18")
        finding = cs.ComplianceViolation(rule_id=other.id, rule_number=other.rule_number, title=other.title,
            category=other.category, severity=other.severity, traffic_light="YELLOW", status="NEEDS_REVIEW",
            flagged_terms=["claim"], explanation="Unrelated source check", penalty=5)
        findings = [finding]
        disclosure.ensure_review(ComplianceCheckRequest(text=POST, content_parts=["imagery"]), [r], findings)
        self.assertEqual(findings, [finding])


class RealPhotoReruns(unittest.IsolatedAsyncioTestCase):
    async def test_real_photos_and_military_caption_do_not_get_rule_nine_or_lose_points(self):
        req = ComplianceCheckRequest(text="Event photographs. " + POST + " #AIPoweredDrones", content_parts=["imagery", "caption"])
        with patch.object(cs, "applicable_rules", AsyncMock(return_value=[synthetic_rule()])), \
             patch.object(cs, "rulebook_version", AsyncMock(return_value="test-rules")), \
             patch.object(cs, "_semantic_findings", AsyncMock(return_value=[])), \
             patch.object(cs.context_evaluator, "evaluate", AsyncMock()), \
             patch.object(cs.source_check_service, "run", AsyncMock(return_value=cs.source_check_service.SourceCheckResult())):
            for _ in range(2):
                result = await cs.check_compliance(req)
                self.assertEqual([f for f in result.violations if f.rule_number == "Rule 9"], [])
                self.assertEqual(result.score, 100)
