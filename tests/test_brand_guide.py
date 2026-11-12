"""The ZenaTech Brand Manual as brand voice for the thought leadership accounts, and how the analysis uses it."""

import unittest

from models.compliance.context_model import ContextEntryIn
from postgresql_db.schemas import setup_context_schema as seeds
from services.compliance.evaluation import context_service
from services.compliance.sentiment import config
from services.compliance.sentiment.input_builder import _reference_context

BRAND_GUIDE = [e for e in seeds.SEED_ENTRIES if e["seed_key"].startswith("brand:CEO_THOUGHT_LEADERSHIP:")]


class BrandGuideSeedTests(unittest.TestCase):
    def test_the_manual_is_four_brand_voice_entries_for_thought_leadership(self):
        self.assertEqual([e["title"] for e in BRAND_GUIDE],
                         ["Brand colours", "Brand typefaces", "ZenaTech logo", "Thought leadership voice"])
        for e in BRAND_GUIDE:
            self.assertEqual(e["kind"], "BRAND_VOICE")
            self.assertEqual(e["brands"], ["CEO_THOUGHT_LEADERSHIP"])

    def test_the_manual_values_are_kept_exactly(self):
        colours = BRAND_GUIDE[0]["body"]
        for value in ("Grand Bleu #005981", "Ottoman Red #ED2224", "Steely Grey #939598"):
            self.assertIn(value, colours)
        self.assertIn("Poppins", BRAND_GUIDE[1]["body"])
        self.assertIn("Montserrat", BRAND_GUIDE[1]["body"])

    def test_entries_fit_the_editor_limits(self):
        # Admins edit these on the Messaging Context page, which validates them as ContextEntryIn.
        for e in BRAND_GUIDE:
            ContextEntryIn(**{k: v for k, v in e.items() if k != "seed_key"})
            self.assertLessEqual(len(e["body"]), 2000, e["title"])
            self.assertLessEqual(len(e["do_examples"]), 1000, e["title"])
            self.assertLessEqual(len(e["dont_examples"]), 1000, e["title"])

    def test_seed_keys_are_unique(self):
        keys = [e["seed_key"] for e in seeds.SEED_ENTRIES]
        self.assertEqual(len(keys), len(set(keys)))


class ThoughtLeadershipScopeTests(unittest.TestCase):
    def test_a_thought_leadership_account_gets_the_guide_under_any_brand(self):
        self.assertEqual(context_service.with_thought_leadership(["ZENATECH"], True),
                         ["ZENATECH", "CEO_THOUGHT_LEADERSHIP"])
        self.assertEqual(context_service.with_thought_leadership(["CEO_THOUGHT_LEADERSHIP"], True),
                         ["CEO_THOUGHT_LEADERSHIP"])
        self.assertEqual(context_service.with_thought_leadership(["ZENATECH"], False), ["ZENATECH"])


class AnalysisUsesTheGuideTests(unittest.TestCase):
    def test_the_prompt_lists_the_guide_with_its_source(self):
        entries = [{"kind": e["kind"], "title": e["title"], "body": e["body"], "do": e["do_examples"],
                    "dont": e["dont_examples"], "source": e["source"], "as_of": None} for e in BRAND_GUIDE]
        text = _reference_context(entries)
        self.assertIn("Brand voice:", text)
        self.assertIn("- [C1] Brand colours (source: ZenaTech Brand Manual): Three main colours.", text)
        self.assertIn("Avoid: Recoloured, stretched", text)

    def test_brand_alignment_checks_the_visual_identity(self):
        self.assertIn("visual identity (colours, typefaces, logo)", config.INSTRUCTIONS)
        self.assertIn("is a BRAND finding with basis OBSERVED_VISUAL", config.INSTRUCTIONS)
        self.assertIn("not held to the palette", config.INSTRUCTIONS)
        self.assertEqual(config.PROMPT_VERSION, "sentiment-v14")


if __name__ == "__main__":
    unittest.main()
