import os
import unittest
from unittest.mock import patch

from services.ai import gemini_service, model_router


class RetiredModelTests(unittest.TestCase):
    def test_retired_models_are_dropped_in_order(self):
        kept = gemini_service.without_retired(["gemini-2.5-flash", "gemini-3.5-flash", "gemini-2.5-flash-lite", "gemini-3.1-flash-lite"])
        self.assertEqual(kept, ["gemini-3.5-flash", "gemini-3.1-flash-lite"])

    def test_a_list_of_only_retired_models_is_left_as_given(self):
        self.assertEqual(gemini_service.without_retired(["gemini-2.5-flash"]), ["gemini-2.5-flash"])

    def test_the_account_list_in_env_cannot_bring_them_back(self):
        env = {"GEMINI_AVAILABLE_MODELS": "Gemini 2.5 Flash,Gemini 3.5 Flash,Gemini 2.5 Flash Lite,Gemini 3.1 Flash Lite"}
        with patch.dict(os.environ, env):
            models = gemini_service.get_gemini_fallback_models()
        self.assertNotIn("gemini-2.5-flash", models)
        self.assertNotIn("gemini-2.5-flash-lite", models)
        self.assertIn("gemini-3.1-flash-lite", models)

    def test_no_route_tries_a_retired_model_first(self):
        for tier in model_router.TIERS:
            self.assertFalse(set(model_router.tier_models(tier)) & gemini_service.DEPRECATED_MODELS, tier)

    def test_a_route_set_in_env_is_filtered_too(self):
        with patch.dict(os.environ, {"AI_ROUTE_TIER_STANDARD": "gemini-2.5-flash,gemini-3-flash-preview"}):
            self.assertEqual(model_router.tier_models("STANDARD"), ["gemini-3-flash-preview"])


if __name__ == "__main__":
    unittest.main()
