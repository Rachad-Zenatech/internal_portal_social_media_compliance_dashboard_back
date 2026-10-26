import unittest
import uuid
from datetime import datetime, timezone
from unittest.mock import AsyncMock, patch

from models.compliance_model import ComplianceCheckRequest, ComplianceRule
from services import compliance_service as cs


def rule(**fields):
    now = datetime.now(timezone.utc)
    data = {
        "id": uuid.uuid4(),
        "rule_number": f"Rule {uuid.uuid4().hex[:4]}",
        "category": "Test",
        "title": "Test rule",
        "summary": "A test rule.",
        "created_at": now,
        "updated_at": now,
    }
    data.update(fields)
    return ComplianceRule(**data)


def padded(text):
    return f" {cs._normalize(text)} "


class PhraseMatchingTests(unittest.TestCase):
    def test_phrases_match_regardless_of_case_and_punctuation(self):
        self.assertTrue(cs._matches("combat proven", padded("Our drone is Combat-Proven!")))
        self.assertFalse(cs._matches("combat proven", padded("Combat provenance matters.")))

    def test_wildcard_stands_for_up_to_three_words(self):
        term = "endorsed by * department of defense"
        self.assertTrue(cs._matches(term, padded("Endorsed by the U.S. Department of Defense")))
        self.assertFalse(cs._matches(term, padded("Endorsed by many many partners in the Department of Defense")))

    def test_an_exception_phrase_cancels_a_match_inside_it(self):
        self.assertFalse(cs._hit("combat proven", padded("This system is not combat proven yet."), ["not combat proven"]))
        # ...but not when the phrase also appears on its own elsewhere.
        self.assertTrue(cs._hit("combat proven", padded("Not combat proven? It is combat proven."), ["not combat proven"]))

    def test_a_disclaimer_counts_when_nearly_word_for_word(self):
        disclaimer = "The appearance of DoD visual information does not imply endorsement"
        self.assertTrue(
            cs._disclaimer_present(disclaimer, padded("The appearance of DOD visual information doesn't imply endorsement"))
        )
        self.assertFalse(cs._disclaimer_present(disclaimer, padded("Visual information from partners")))


class RuleScopeTests(unittest.TestCase):
    def test_a_rule_applies_unless_limited_to_other_values(self):
        scoped = rule(brands=["ZENADRONE"], platforms=["LINKEDIN"])
        self.assertTrue(cs._rule_applies(scoped, ComplianceCheckRequest(text="x", brand_code="ZENADRONE", platforms=["LINKEDIN"])))
        self.assertFalse(cs._rule_applies(scoped, ComplianceCheckRequest(text="x", brand_code="ZENATECH", platforms=["LINKEDIN"])))
        # Content that doesn't say (no brand yet) is checked against the rule.
        self.assertTrue(cs._rule_applies(scoped, ComplianceCheckRequest(text="x", platforms=["LINKEDIN"])))


class ScoringTests(unittest.IsolatedAsyncioTestCase):
    async def check(self, rules, text, **scope):
        """check_compliance against exactly these rules (no database)."""

        async def applicable(req):
            return [r for r in rules if cs._rule_applies(r, req)]

        with (
            patch("services.compliance_service.applicable_rules", new=AsyncMock(side_effect=applicable)),
            patch("services.compliance_service.rulebook_version", new=AsyncMock(return_value=None)),
        ):
            return await cs.check_compliance(ComplianceCheckRequest(text=text, **scope))

    async def test_clean_text_scores_100(self):
        result = await self.check([rule(prohibited_terms=["guaranteed returns"])], "Meet the team at the expo.")
        self.assertEqual((result.score, result.traffic_light, result.blocked), (100, "GREEN", False))

    async def test_each_finding_takes_its_penalty_off_100(self):
        rules = [
            rule(severity="HIGH", traffic_light="YELLOW", prohibited_terms=["fully autonomous"]),
            rule(severity="MEDIUM", traffic_light="GREEN", prohibited_terms=["best in class"]),
        ]
        result = await self.check(rules, "A fully autonomous drone, best in class.")
        self.assertEqual(result.score, 100 - cs.PENALTY_HIGH - cs.PENALTY_OTHER)
        self.assertEqual(result.traffic_light, "YELLOW")
        self.assertFalse(result.blocked)

    async def test_a_critical_finding_blocks_and_caps_the_score(self):
        rules = [rule(severity="CRITICAL", traffic_light="RED", prohibited_terms=["deployed in combat"])]
        result = await self.check(rules, "Our drone has been deployed in combat.")
        self.assertTrue(result.blocked)
        self.assertEqual(result.traffic_light, "RED")
        self.assertEqual(result.raw_score, 100 - cs.PENALTY_CRITICAL)
        self.assertEqual(result.score, cs.CRITICAL_SCORE_CAP)
        self.assertEqual(result.score_cap, cs.CRITICAL_SCORE_CAP)

    async def test_a_missing_disclaimer_is_a_yellow_finding(self):
        rules = [rule(required_disclaimers=["Concept rendering, not an operational system"], disclaimer_triggers=["concept"])]
        result = await self.check(rules, "A concept of our next drone.")
        self.assertEqual(result.violations_count, 1)
        self.assertEqual(result.violations[0].kind, "MISSING_DISCLAIMER")
        self.assertEqual(result.traffic_light, "YELLOW")
        self.assertEqual(result.score, 100 - cs.PENALTY_MISSING_DISCLAIMER)

        with_it = await self.check(rules, "A concept of our next drone. Concept rendering, not an operational system.")
        self.assertTrue(with_it.is_compliant)

    async def test_the_same_words_flagged_by_two_rules_cost_points_once(self):
        rules = [
            rule(rule_number="Rule A", severity="CRITICAL", traffic_light="RED", prohibited_terms=["combat ready"]),
            rule(rule_number="Rule B", severity="HIGH", traffic_light="YELLOW", prohibited_terms=["combat ready"]),
        ]
        result = await self.check(rules, "Combat ready today.")
        self.assertEqual({v.rule_number: v.penalty for v in result.violations}, {"Rule A": cs.PENALTY_CRITICAL, "Rule B": 0})
        self.assertEqual(result.raw_score, 100 - cs.PENALTY_CRITICAL)

    async def test_rules_for_other_rule_sets_are_skipped(self):
        rules = [rule(rule_sets=["ZenaDrone Defense"], severity="CRITICAL", traffic_light="RED", prohibited_terms=["kill chain"])]
        self.assertTrue((await self.check(rules, "Shortening the kill chain.", rule_sets=["ZenaTech General"])).is_compliant)
        self.assertFalse((await self.check(rules, "Shortening the kill chain.", rule_sets=["ZenaDrone Defense"])).is_compliant)


class ManualReviewRuleTests(unittest.IsolatedAsyncioTestCase):
    async def test_rules_with_nothing_to_look_for_in_text_are_listed_not_passed(self):
        image_rule = rule(rule_number="Rule 20", title="Image editing", severity="CRITICAL", traffic_light="RED")
        phrase_rule = rule(rule_number="Rule 24", prohibited_terms=["guaranteed"])
        disclaimer_rule = rule(rule_number="Rule 4", required_disclaimers=["No endorsement implied"], disclaimer_triggers=["army"])

        with (
            patch("services.compliance_service.applicable_rules", new=AsyncMock(return_value=[image_rule, phrase_rule, disclaimer_rule])),
            patch("services.compliance_service.rulebook_version", new=AsyncMock(return_value=None)),
        ):
            result = await cs.check_compliance(ComplianceCheckRequest(text="Meet the team."))

        self.assertEqual(result.score, 100)
        self.assertEqual(result.rules_checked, 3)
        self.assertEqual([m.rule_number for m in result.manual_review_rules], ["Rule 20"])


if __name__ == "__main__":
    unittest.main()
