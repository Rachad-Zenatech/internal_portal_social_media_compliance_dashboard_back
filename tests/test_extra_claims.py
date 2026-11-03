import unittest

from tests import test_compliance_scoring as base
from tests.test_compliance_scoring import rule, setUpModule, tearDownModule  # noqa: F401  (module-level patches)

ABSOLUTES = ["guarantees * accuracy", "100 * accuracy", "never misses", "never makes mistakes", "perfect results", "every single time"]


class ExtraClaimTests(unittest.IsolatedAsyncioTestCase):
    """Uses the scoring tests' no-database check helper."""

    check = base.ScoringTests.check

    def rules(self):
        return [rule(rule_number="Rule X", severity="HIGH", traffic_light="YELLOW", prohibited_terms=ABSOLUTES)]

    async def test_one_claim_costs_the_rule_penalty_only(self):
        result = await self.check(self.rules(), "Our drone guarantees 100% inventory accuracy.")
        self.assertEqual(result.score, 80)

    async def test_overlapping_phrases_are_one_claim(self):
        # "guarantees * accuracy" and "100 * accuracy" both match these same words.
        result = await self.check(self.rules(), "It guarantees 100% inventory accuracy.")
        self.assertEqual(result.score, 80)

    async def test_a_second_separate_claim_costs_five_more(self):
        result = await self.check(self.rules(), "It guarantees 100% inventory accuracy. It never misses.")
        self.assertEqual(result.score, 75)

    async def test_extra_claims_add_a_little_each(self):
        text = "It never misses. It never makes mistakes. It gives perfect results."
        self.assertEqual((await self.check(self.rules(), text)).score, 70)

    async def test_extra_claims_are_capped(self):
        text = "It guarantees 100% accuracy. It never misses. It never makes mistakes. It gives perfect results. Works every single time."
        self.assertEqual((await self.check(self.rules(), text)).score, 70)

    async def test_a_denied_phrase_is_not_a_claim(self):
        text = "It never misses a beat on setup. We do not promise perfect results."
        result = await self.check(self.rules(), text)
        self.assertEqual(result.score, 80)


if __name__ == "__main__":
    unittest.main()
