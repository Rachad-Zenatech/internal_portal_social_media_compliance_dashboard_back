"""Military topic references must not become government-imagery disclaimer requirements."""
import json
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from models.compliance.compliance_model import ComplianceCheckRequest
from postgresql_db.schemas.setup_compliance_schema import BASE_RULES, BASE_DISCLAIMER_TRIGGERS
from services.compliance.evaluation import compliance_service as cs
from tests.test_baseline_rule_terms import rule_from_seed


EXAMPLE = """Text in image 1:
NASDAQ: ZENA
2-WEEK U.S. ARMY ASSESSMENT
POWER THROUGH THE TETHER
CONTINUOUS POWER AND DATA THROUGH A PHYSICAL CONNECTION.
PERSISTENT SURVEILLANCE
POWER + DATA
Illustrative schematic, not assessed hardware.
I believe the mission should determine how a drone is powered.
Source: U.S. Army / September 2, 2026 / army.mil/article/295038
Two weeks describes the assessment, not continuous flight duration.
"""


def defense_rules():
    rules = [rule_from_seed(seed) for seed in BASE_RULES if seed["rule_number"] in {"Rule 4", "Rule 13"}]
    for rule in rules:
        rule.disclaimer_triggers = BASE_DISCLAIMER_TRIGGERS[rule.rule_number]
    return rules


class DefenseDisclaimerTests(unittest.TestCase):
    def test_plain_mentions_citations_and_schematics_do_not_require_visual_disclaimers(self):
        for rule in defense_rules():
            for text in [EXAMPLE, "The Army published an assessment.", "Source: U.S. Army / army.mil/article/123"]:
                with self.subTest(rule=rule.rule_number, text=text):
                    normalized = f" {cs._normalize(cs.without_image_marks(text))} "
                    self.assertIsNone(cs._evaluate_rule(rule, normalized))

    def test_actual_government_visuals_still_require_review(self):
        for rule in defense_rules():
            for text in ["Official Army footage", "Photograph from the U.S. Navy", "DVIDS video", "Army logo"]:
                with self.subTest(rule=rule.rule_number, text=text):
                    finding = cs._evaluate_rule(rule, f" {cs._normalize(text)} ")
                    self.assertIsNotNone(finding)
                    self.assertEqual(finding.kind, "MISSING_DISCLAIMER")

    def test_actual_endorsement_claims_are_still_violations(self):
        finding = cs._evaluate_rule(defense_rules()[0], " approved by the army ")
        self.assertEqual(finding.kind, "PROHIBITED_TERM")
        self.assertEqual(finding.penalty, 35)

    def test_custom_rules_and_other_disclaimers_keep_their_requirements(self):
        rule = defense_rules()[0]
        rule.is_custom = True
        self.assertIsNotNone(cs._evaluate_rule(rule, " army "))
        rule.is_custom = False
        rule.required_disclaimers = ["A custom disclosure"]
        self.assertIsNotNone(cs._evaluate_rule(rule, " army "))


class DefenseDisclaimerPipelineTests(unittest.IsolatedAsyncioTestCase):
    async def test_the_submitted_schematic_is_cleared_only_after_context_review(self):
        source_result = SimpleNamespace(ai_failed=False, checks=[])
        async def clear(candidates, *args, **kwargs):
            for candidate in candidates:
                cs.context_evaluator._apply(candidate, cs.context_evaluator._Result(
                    id="0", status="NO_VIOLATION", matched_text="Illustrative schematic, not assessed hardware.",
                    reasoning="The full post describes a third-party assessment and an illustrative schematic.",
                    confidence=0.95,
                ), "test-model", False)
        with (
            patch.object(cs, "applicable_rules", AsyncMock(return_value=defense_rules())),
            patch.object(cs, "_semantic_findings", AsyncMock(return_value=[])),
            patch.object(cs.source_check_service, "run", AsyncMock(return_value=source_result)),
            patch.object(cs.source_post_checks, "unsourced_image_findings", return_value=[]),
            patch.object(cs.context_evaluator, "evaluate", AsyncMock(side_effect=clear)) as evaluate,
            patch.object(cs, "rulebook_version", AsyncMock(return_value="test")),
        ):
            result = await cs.check_compliance(ComplianceCheckRequest(
                text=EXAMPLE, audiences=["DEFENSE"], content_type="IMAGE",
            ))
        self.assertEqual(len(evaluate.await_args.args[0]), 1)
        self.assertEqual(result.violations[0].status, "CLEARED")
        self.assertEqual(result.violations[0].evaluation.model, "test-model")
        self.assertEqual(result.score, 100)
        self.assertEqual(result.review_count, 0)
        self.assertFalse(result.blocked)

    async def test_semantic_matching_cannot_reintroduce_a_disclaimer_for_a_name_alone(self):
        rule = defense_rules()[0]
        answer = {"semantic_violations": [{
            "rule_number": "Rule 4", "quote": "U.S. ARMY", "confidence": 0.9,
            "explanation": "Check whether a disclaimer is required.", "kind": "MISSING_DISCLAIMER",
        }]}
        with (
            patch.object(cs, "_ai_viability_enabled", return_value=True),
            patch.object(cs, "_get_gemini_client", return_value=object()),
            patch.object(cs, "_vector_context_for_check", AsyncMock(return_value=([rule], []))),
            patch.object(cs._ai_check_cache, "get", AsyncMock(return_value=None)),
            patch.object(cs._ai_check_cache, "set", AsyncMock()),
            patch.object(cs, "generate_content_with_fallback", AsyncMock(return_value=SimpleNamespace(text=json.dumps(answer)))),
        ):
            findings = await cs._semantic_findings(ComplianceCheckRequest(text=EXAMPLE), [rule], set(),
                                                   f" {cs._normalize(cs.without_image_marks(EXAMPLE))} ")
        self.assertEqual(findings, [])
