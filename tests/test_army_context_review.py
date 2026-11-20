"""Army wording always reaches AI; its context, rather than the mention, determines the result."""
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from models.compliance.compliance_model import ComplianceCheckRequest
from services.compliance.evaluation import army_context_review as army, compliance_service as cs, context_evaluator as ce
from tests.test_defense_disclaimer import EXAMPLE, defense_rules


REVISED = """Text in image 1:
TETHERED DRONES / INDUSTRY PERSPECTIVE
POWER THE MISSION / RETHINK THE CONNECTION
Conceptual illustration. Not assessed hardware.
Industry commentary only. No U.S. Army evaluation or endorsement of ZenaTech is claimed.
Source: U.S. Army / army.mil/article/295038
"""


class ArmyRoutingTests(unittest.IsolatedAsyncioTestCase):
    async def test_army_in_any_part_brings_in_active_rule4_despite_scope(self):
        rule = defense_rules()[0]
        rule.rule_sets = ["ZenaDrone Defense"]
        for text in ("Army", "The Army published a report.", "Source: U.S. Army / army.mil/article/295038", "Thanks @USArmy"):
            with self.subTest(text=text), patch.object(cs, "list_compliance_rules", AsyncMock(return_value=[rule])):
                req = ComplianceCheckRequest(text=text, rule_sets=["ZenaTech General"], audiences=["GENERAL"])
                self.assertIn(rule, await cs.applicable_rules(req))

    def test_no_army_mention_does_not_add_a_candidate(self):
        candidates, findings = [], []
        army.include(ComplianceCheckRequest(text="A commercial drone diagram."), defense_rules(), findings, candidates)
        self.assertEqual((findings, candidates), ([], []))

    def test_context_contains_the_entire_post_disclosures_and_audience(self):
        candidates, findings = [], []
        req = ComplianceCheckRequest(text=REVISED, audiences=["DEFENSE"], content_type="SOCIAL_POST")
        army.include(req, defense_rules(), findings, candidates)
        self.assertEqual(len(candidates), 1)
        item = ce._item(0, candidates[0])
        self.assertEqual(item["context"], REVISED)
        self.assertEqual(item["submission_details"]["audiences"], ["DEFENSE"])
        self.assertTrue(candidates[0].trigger_only)
        self.assertEqual(findings[0].penalty, 0)
        self.assertNotEqual(findings[0].status, "VIOLATION")

    def test_an_existing_rule4_candidate_is_not_duplicated(self):
        rule = defense_rules()[0]
        finding = cs._evaluate_rule(rule, " approved by the army ")
        candidates, findings = [ce.Candidate(finding, rule, "Army", "Army")], [finding]
        army.include(ComplianceCheckRequest(text="Our drone is approved by the Army."), [rule], findings, candidates)
        self.assertEqual(len(candidates), 1)
        self.assertEqual(len(findings), 1)
        self.assertFalse(candidates[0].trigger_only)

    def candidate(self, text=REVISED):
        candidates = []
        army.include(ComplianceCheckRequest(text=text), defense_rules(), [], candidates)
        return candidates[0]

    def test_unavailable_ai_does_not_turn_a_mention_into_a_critical_violation(self):
        candidate = self.candidate()
        ce._apply(candidate, None, None, False)
        self.assertEqual(candidate.finding.status, "NEEDS_REVIEW")
        self.assertEqual(candidate.finding.penalty, 0)
        self.assertEqual(candidate.finding.evaluation.source, "UNAVAILABLE")

    def test_ai_can_raise_a_real_endorsement_violation_with_normal_points(self):
        candidate = self.candidate()
        ce._apply(candidate, ce._Result(id="0", status="VIOLATION", matched_text="U.S. Army",
                                       reasoning="A confirmed endorsement claim.", confidence=0.95), "test-model", False)
        self.assertEqual(candidate.finding.status, "VIOLATION")
        self.assertEqual(candidate.finding.penalty, 35)
        self.assertEqual(candidate.finding.traffic_light, "RED")

    def test_cleared_context_is_saved_even_when_the_caption_calls_the_post_a_test(self):
        candidate = self.candidate("test\n" + REVISED)
        ce._apply(candidate, ce._Result(id="0", status="NO_VIOLATION", matched_text="No U.S. Army evaluation",
                                       reasoning="The post expressly denies an Army evaluation.", confidence=0.95), "test-model", False)
        self.assertEqual(candidate.finding.status, "CLEARED")
        self.assertEqual(candidate.finding.evaluation.rule_decision, "NEEDS_REVIEW")

    def test_changed_disclosures_or_audience_change_the_context_cache_key(self):
        candidate = self.candidate()
        initial = ce._key(candidate)
        candidate.context += "\nThe Army evaluated our hardware."
        self.assertNotEqual(initial, ce._key(candidate))
        initial = ce._key(candidate)
        candidate.review_metadata["audiences"] = ["INVESTORS"]
        self.assertNotEqual(initial, ce._key(candidate))


class ArmyPipelineTests(unittest.IsolatedAsyncioTestCase):
    async def test_both_versions_reach_context_review_and_save_the_actual_ai_decision(self):
        for text, decision, expected_points in ((EXAMPLE, "NEEDS_REVIEW", 18), (REVISED, "NO_VIOLATION", 0)):
            with self.subTest(decision=decision):
                async def judge(batch, guard):
                    return {i: ce._Result(id=str(i), status=decision, matched_text="U.S. Army",
                                          reasoning="Judged from the complete post.", confidence=0.9)
                            for i, _ in batch}, "context-test-model"
                with (
                    patch.object(cs, "applicable_rules", AsyncMock(return_value=defense_rules())),
                    patch.object(cs, "_semantic_findings", AsyncMock(return_value=[])),
                    patch.object(cs.source_check_service, "run", AsyncMock(return_value=SimpleNamespace(ai_failed=False, checks=[]))),
                    patch.object(cs.source_post_checks, "unsourced_image_findings", return_value=[]),
                    patch.object(cs.synthetic_media_disclosure, "ensure_review"),
                    patch.object(cs, "rulebook_version", AsyncMock(return_value="test")),
                    patch.object(ce.finding_precedent_service, "similar", AsyncMock(return_value=[])),
                    patch.object(ce._cache, "get", AsyncMock(return_value=None)),
                    patch.object(ce._cache, "set", AsyncMock()),
                    patch.object(ce, "_judge", AsyncMock(side_effect=judge)) as ai,
                ):
                    result = await cs.check_compliance(ComplianceCheckRequest(text=text, audiences=["DEFENSE"]))
                ai.assert_awaited_once()
                self.assertEqual(ai.await_args.args[0][0][1].context, text)
                finding = next(v for v in result.violations if v.rule_number == "Rule 4")
                self.assertEqual(finding.penalty, expected_points)
                self.assertEqual(finding.evaluation.model, "context-test-model")
                self.assertEqual(finding.status, "CLEARED" if decision == "NO_VIOLATION" else decision)
