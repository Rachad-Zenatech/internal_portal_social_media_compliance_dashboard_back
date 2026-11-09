import os
import unittest
from unittest.mock import AsyncMock, patch
from uuid import uuid4

from models.compliance.compliance_model import ComplianceCheckRequest, ComplianceRule
from postgresql_db.schemas import setup_compliance_schema as schema
from services.compliance.evaluation import compliance_service as cs


class ClaimGateTests(unittest.TestCase):
    def test_posts_that_make_claims_are_worth_the_extra_pass(self):
        for text in (
            "ZenaDrone is the best drone platform for defense, period.",
            "Automation reduces errors and strengthens audit readiness.",
            "ZenaDrone's swarm system autonomously guards every border.",
            "Our counting drone is 99.9% accurate.",
            "We just signed Walmart as a customer.",
            "Proud to see ZenaDrone selected by the U.S. Army for field trials.",
            "We anticipate opening two new locations by Q3 2027.",
        ):
            self.assertTrue(cs._looks_like_claims(text), text)

    def test_a_greeting_or_an_event_notice_is_not(self):
        for text in (
            "Happy Thanksgiving from the ZenaTech family!",
            "Meet the ZenaTech team at the investor conference next week. Details at zenatech.com/ir.",
            "Thank you to everyone who visited our booth.",
        ):
            self.assertFalse(cs._looks_like_claims(text), text)

    def test_the_mode_comes_from_the_environment(self):
        for raw, mode in (("", "off"), ("off", "off"), ("auto", "auto"), ("always", "always"), ("true", "always"), ("false", "off")):
            with patch.dict(os.environ, {"COMPLIANCE_FULL_RULE_PASS": raw}):
                self.assertEqual(cs._full_rule_mode(), mode, raw)


class SentenceSplitTests(unittest.TestCase):
    def test_long_enough_sentences_once_in_order(self):
        text = "Great week at the logistics show. Yes. We have a very long sentence here about the new platform. Yes. We have a very long sentence here about the new platform."
        self.assertEqual(cs._sentences(text, 10), ["Great week at the logistics show.", "We have a very long sentence here about the new platform."])

    def test_it_stops_at_the_limit(self):
        text = " ".join(f"This is sentence number {i} of the post." for i in range(30))
        self.assertEqual(len(cs._sentences(text, 5)), 5)


def rule(number):
    from datetime import datetime, timezone

    now = datetime.now(timezone.utc)
    return ComplianceRule(
        id=uuid4(), rule_number=number, category="c", title=f"{number} t", summary="s", created_at=now, updated_at=now,
        severity="MEDIUM", traffic_light="YELLOW",
    )


class SentenceSearchTests(unittest.IsolatedAsyncioTestCase):
    async def test_a_rule_found_only_by_one_sentence_is_picked(self):
        r16, r24 = rule("Rule 16"), rule("Rule 24")
        calls = []

        async def search(query, **kw):
            calls.append(query)
            # The whole post matches nothing; one sentence matches Rule 24.
            if query.startswith("ZenaDrone is the best drone platform"):
                return [{"source_id": str(r24.id), "meta": {"rule_number": "Rule 24"}, "score": 0.8}]
            return []

        text = "Great week meeting customers at the show. ZenaDrone is the best drone platform for defense, period."
        with (
            patch.object(cs.store, "is_enabled", return_value=True),
            patch.object(cs.store, "search", new=search),
        ):
            rules, _ = await cs._vector_context_for_check(ComplianceCheckRequest(text=text), [r16, r24], set())
        self.assertEqual([r.rule_number for r in rules], ["Rule 24"])
        self.assertGreater(len(calls), 1)  # the whole post, plus its sentences


class TriggerPhraseTests(unittest.TestCase):
    def test_soft_claim_phrases_are_added_to_the_rules(self):
        added = schema._ADDED_BASELINE_TERMS
        self.assertIn("best * platform", added["Rule 24"])
        self.assertIn("reduces errors", added["Rule 24"])
        self.assertIn("guards every", added["Rule 16"])

    def test_none_is_so_generic_it_would_match_everything(self):
        for phrases in schema._ADDED_BASELINE_TERMS.values():
            for phrase in phrases:
                self.assertGreaterEqual(len(phrase), 6, phrase)


if __name__ == "__main__":
    unittest.main()
