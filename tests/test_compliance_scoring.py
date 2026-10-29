import unittest
import uuid
from datetime import datetime, timezone
from unittest.mock import AsyncMock, patch

from fastapi import HTTPException

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


class PostSignalScoringTests(unittest.IsolatedAsyncioTestCase):
    check = ScoringTests.check

    async def test_a_phrase_in_a_camel_case_hashtag_is_flagged(self):
        rules = [rule(severity="HIGH", traffic_light="YELLOW", prohibited_terms=["combat proven"])]
        result = await self.check(rules, "Our new drone. #CombatProven")
        self.assertEqual(result.violations[0].flagged_terms, ["combat proven"])

    async def test_a_phrase_run_together_in_a_hashtag_is_flagged(self):
        rules = [rule(severity="HIGH", traffic_light="YELLOW", prohibited_terms=["combat proven"],
                      exception_terms=["not combat proven"])]
        self.assertFalse((await self.check(rules, "Our new drone. #combatproven")).is_compliant)
        # An exception run together the same way still excuses it.
        self.assertTrue((await self.check(rules, "Our new drone. #notcombatproven")).is_compliant)

    async def test_a_hashtag_can_trigger_a_required_disclaimer(self):
        rules = [rule(required_disclaimers=["Concept rendering, not an operational system"],
                      disclaimer_triggers=["concept art"])]
        result = await self.check(rules, "The next generation. #conceptart")
        self.assertEqual(result.violations[0].kind, "MISSING_DISCLAIMER")

    async def test_tagging_the_military_is_a_yellow_finding(self):
        from services import post_signals
        result = await self.check([], "Great demo with @USArmy today.")
        self.assertEqual([v.rule_id for v in result.violations], [post_signals.GOVERNMENT_REFERENCE_ID])
        self.assertEqual(result.traffic_light, "YELLOW")
        self.assertEqual(result.signals.government_references, ["@USArmy"])

    async def test_a_missing_dod_disclaimer_and_a_tag_count_once(self):
        disclaimer = "The appearance of U.S. Department of Defense visual information does not imply or constitute DOD endorsement."
        rules = [rule(rule_number="Rule 4", required_disclaimers=[disclaimer], disclaimer_triggers=["army"])]
        result = await self.check(rules, "Great demo with the army today. @USArmy")
        self.assertEqual([v.rule_number for v in result.violations], ["Rule 4"])

    async def test_signals_and_notes_are_returned(self):
        result = await self.check([], "Big week for $ZENA. Read more at defensenews.com #a #b #c #d #e #f")
        self.assertEqual(result.signals.cashtags, ["$ZENA"])
        self.assertEqual(len(result.signals.hashtags), 6)
        # The cashtag means investors read it (first), then the outside link and the hashtag wall.
        self.assertEqual(len(result.signals.notes), 3)
        self.assertIn("investors will read this post", result.signals.notes[0])
        self.assertEqual(result.signals.implied_audiences, ["INVESTORS"])
        # Notes cost nothing.
        self.assertEqual(result.score, 100)


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


class AppliesToTests(unittest.TestCase):
    def test_content_types_map_to_the_parts_rules_name(self):
        self.assertEqual(cs.parts_for("VIDEO_FACEBOOK"), ["caption", "video"])
        self.assertEqual(cs.parts_for("CAROUSEL"), ["caption", "imagery"])
        self.assertEqual(cs.parts_for("SOCIAL_POST"), ["caption"])
        self.assertEqual(cs.parts_for("imagery_concept"), ["caption", "imagery"])
        # What's attached counts too: a text post with an image has imagery.
        self.assertEqual(cs.parts_for("SOCIAL_POST", has_images=True), ["caption", "imagery"])
        # An ad is what its creative is, or could be either before it's attached.
        self.assertEqual(cs.parts_for("AD", has_images=True), ["caption", "imagery"])
        self.assertEqual(cs.parts_for("AD"), ["caption", "imagery", "video"])
        self.assertEqual(cs.parts_for("BLOG", is_ceo=True), ["caption", "ceo_post"])
        # Nothing known: no rule is left out.
        self.assertEqual(cs.parts_for(None), [])

    def test_a_rule_limited_to_video_skips_image_posts(self):
        video_only = rule(applies_to=["video"])
        self.assertTrue(cs._rule_applies(video_only, ComplianceCheckRequest(text="x", content_type="VIDEO")))
        self.assertFalse(cs._rule_applies(video_only, ComplianceCheckRequest(text="x", content_type="IMAGE")))
        self.assertFalse(cs._rule_applies(video_only, ComplianceCheckRequest(text="x", content_parts=["caption", "imagery"])))
        # Content that doesn't say what it is is checked against the rule.
        self.assertTrue(cs._rule_applies(video_only, ComplianceCheckRequest(text="x", content_type=None)))

    def test_a_ceo_only_rule_runs_on_shauns_posts_only(self):
        ceo_only = rule(applies_to=["ceo_post"])
        self.assertFalse(cs._rule_applies(ceo_only, ComplianceCheckRequest(text="x", content_type="SOCIAL_POST", brand_code="ZENATECH")))
        self.assertTrue(cs._rule_applies(ceo_only, ComplianceCheckRequest(text="x", content_type="SOCIAL_POST", brand_code=cs.CEO_BRAND)))
        self.assertTrue(cs._rule_applies(ceo_only, ComplianceCheckRequest(text="x", content_type="SOCIAL_POST", rule_sets=[cs.CEO_RULE_SET])))

    def test_default_rules_apply_to_everything(self):
        default = rule()
        for content_type in ("VIDEO", "IMAGE", "SOCIAL_POST", "BLOG", "AD", None):
            with self.subTest(content_type=content_type):
                self.assertTrue(cs._rule_applies(default, ComplianceCheckRequest(text="x", content_type=content_type)))

    def test_applies_to_is_validated(self):
        self.assertEqual(cs._clean_applies_to([" Video ", "video", "CEO_POST"]), ["video", "ceo_post"])
        with self.assertRaises(HTTPException):
            cs._clean_applies_to(["script"])


class CashtagAudienceTests(unittest.IsolatedAsyncioTestCase):
    check = ScoringTests.check
    investor_rule = rule(rule_number="Rule IR", audiences=["INVESTORS"], prohibited_terms=["guaranteed returns"])

    async def test_a_cashtag_brings_in_the_investor_rules(self):
        text = "Guaranteed returns ahead for $ZENA holders."
        without = await self.check([self.investor_rule], "Guaranteed returns ahead for our holders.", audience="GENERAL")
        self.assertEqual(without.violations_count, 0)

        result = await self.check([self.investor_rule], text, audience="GENERAL")
        self.assertEqual([v.rule_number for v in result.violations], ["Rule IR"])
        self.assertEqual(result.audiences_checked, ["GENERAL", "INVESTORS"])
        self.assertEqual(result.signals.implied_audiences, ["INVESTORS"])
        self.assertTrue(result.signals.notes[0].startswith("$ZENA is about the stock"))

    async def test_no_audience_still_checks_every_audience(self):
        defense_rule = rule(audiences=["DEFENSE"], prohibited_terms=["combat proven"])
        result = await self.check([self.investor_rule, defense_rule], "$ZENA: combat proven, guaranteed returns.")
        # Adding investors mustn't narrow the check to them.
        self.assertEqual(result.violations_count, 2)
        self.assertEqual(result.audiences_checked, [])
        self.assertEqual(result.signals.implied_audiences, ["INVESTORS"])

    async def test_investor_posts_need_no_note(self):
        result = await self.check([self.investor_rule], "$ZENA update.", audience="INVESTORS")
        self.assertEqual(result.signals.implied_audiences, [])
        self.assertEqual(result.audiences_checked, ["INVESTORS"])


if __name__ == "__main__":
    unittest.main()
