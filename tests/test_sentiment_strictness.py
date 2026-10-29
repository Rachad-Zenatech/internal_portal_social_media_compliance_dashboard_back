import unittest

from services.compliance import sentiment_service as s


def inp(audience="GENERAL", rule_sets=("ZenaTech General",), is_ceo_channel=False, controversy_dial=False):
    return {
        "title": "Spooky season",
        "caption": "Waiting for our next drone delivery like...",
        "brand": "ZenaTech",
        "audience": audience,
        "region": "Global",
        "platforms": ["TIKTOK"],
        "rule_sets": list(rule_sets),
        "channel": "ZenaTech (TikTok)",
        "is_ceo_channel": is_ceo_channel,
        "controversy_dial": controversy_dial,
        "transcript": None,
        "images": [],
        "guidelines": [],
        "context": [],
    }


class StrictnessTests(unittest.TestCase):
    def test_everyday_posts_are_read_as_trends(self):
        for audience in ("GENERAL", "COMMERCIAL", None):
            with self.subTest(audience=audience):
                self.assertFalse(s._held_to_full_standard(inp(audience=audience)))
                prompt = s._prompt(inp(audience=audience))
                self.assertIn(s.RELAXED_GUIDE, prompt)
                self.assertNotIn(s.STRICT_GUIDE, prompt)

    def test_defense_investor_and_ceo_content_keep_the_full_standard(self):
        for case in (
            inp(audience="DEFENSE"),
            inp(audience="INVESTORS"),
            inp(is_ceo_channel=True),
            inp(controversy_dial=True),
            inp(rule_sets=("ZenaDrone Defense",)),
        ):
            with self.subTest(case=case):
                self.assertTrue(s._held_to_full_standard(case))
                self.assertIn(s.STRICT_GUIDE, s._prompt(case))

    def test_a_cashtag_holds_a_general_post_to_the_investor_standard(self):
        case = {**inp(audience="GENERAL"), "implied_audiences": ["INVESTORS"], "cashtags": ["$ZENA"]}
        self.assertTrue(s._held_to_full_standard(case))
        prompt = s._prompt(case)
        self.assertIn(s.STRICT_GUIDE, prompt)
        self.assertIn("it also reaches investors: it mentions $ZENA", prompt)

    def test_relaxed_style_never_relaxes_claims(self):
        self.assertIn("Claims, figures, disclosure and the rulebook guidelines still apply in full", s.RELAXED_GUIDE)


if __name__ == "__main__":
    unittest.main()
