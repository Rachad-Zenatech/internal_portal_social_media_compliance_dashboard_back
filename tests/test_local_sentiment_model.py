"""The trained local sentiment model (with VADER as the fallback) for scraped X posts and headlines."""

import unittest
from unittest.mock import patch

from services.compliance.analytics import listening_providers as lp

DEFENCE = "Army contract to kill enemy drone threats awarded to Kratos, stock up"


class FallbackTests(unittest.TestCase):
    def test_vader_is_used_when_the_model_is_switched_off(self):
        with patch.object(lp, "_local_model", False):
            res = lp.classify_comment_sentiment("Terrible customer service. I want a refund.")
        self.assertEqual(res["sentiment"], "NEGATIVE")
        self.assertNotIn("model", res)

    def test_vader_is_used_when_the_model_fails_on_a_text(self):
        with patch.object(lp, "_model_probs", return_value=None):
            res = lp.classify_comment_sentiment("This product is amazing. Great service!")
        self.assertEqual(res["sentiment"], "POSITIVE")

    def test_model_result_is_used_and_crisis_words_need_a_negative_reading(self):
        probs = {"POSITIVE": 0.1, "NEUTRAL": 0.8, "NEGATIVE": 0.1}
        with patch.object(lp, "_model_probs", return_value=probs):
            res = lp.classify_comment_sentiment(DEFENCE)
        self.assertEqual(res["sentiment"], "NEUTRAL")
        self.assertEqual(res["crisis_flags"], [])  # "kill", "enemy" are in VADER's list, but it's a contract headline
        self.assertEqual(res["model"], lp.LOCAL_MODEL_NAME)

        probs = {"POSITIVE": 0.02, "NEUTRAL": 0.08, "NEGATIVE": 0.9}
        with patch.object(lp, "_model_probs", return_value=probs):
            res = lp.classify_comment_sentiment("Investors furious, a terrible disaster")
        self.assertEqual(res["sentiment"], "NEGATIVE")
        self.assertTrue(res["crisis_flags"])


class RealModelTests(unittest.TestCase):
    """Uses the downloaded model; skipped when it isn't installed or can't be fetched."""

    @classmethod
    def setUpClass(cls):
        lp._local_model = None
        if not lp._load_local_model():
            raise unittest.SkipTest("trained sentiment model isn't available")

    def test_defence_headline_is_not_a_crisis(self):
        res = lp.classify_comment_sentiment(DEFENCE)
        self.assertNotEqual(res["sentiment"], "NEGATIVE")
        self.assertEqual(res["crisis_flags"], [])

    def test_clear_positive_and_negative(self):
        self.assertEqual(lp.classify_comment_sentiment("Great progress, I love this!")["sentiment"], "POSITIVE")
        self.assertEqual(lp.classify_comment_sentiment("Not impressed. A joke and a waste of money")["sentiment"], "NEGATIVE")


if __name__ == "__main__":
    unittest.main()
