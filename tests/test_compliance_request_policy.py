"""Latency and fallback coverage tests; all provider calls are mocked."""
import os
import time
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch
from google.genai import types
from services.ai.core import compliance_request_policy as policy, gemini_service as gemini
from services.ai.observability import ai_observability as obs
from services.compliance.evaluation import context_evaluator as context


class ComplianceRequestPolicyTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        gemini._model_cooldowns.clear()
        self.env = patch.dict(os.environ, {"COMPLIANCE_MODEL_TIMEOUT_SEC": "20", "VISUAL_COMPLIANCE_TIMEOUT_SEC": "30"})
        self.env.start()

    def tearDown(self):
        self.env.stop()
        gemini._model_cooldowns.clear()

    async def test_real_call_boundary_has_one_sdk_attempt_and_preserves_review_schema(self):
        client = MagicMock()
        client.models.generate_content.return_value = SimpleNamespace(text="{}", usage_metadata=None)
        config = types.GenerateContentConfig(system_instruction="Keep every applicable rule", temperature=0,
            response_mime_type="application/json", response_schema={"type": "OBJECT"},
            http_options=types.HttpOptions(timeout=60000))
        with patch.object(obs, "log_prompt"):
            await obs.generate(client, model="test-model", contents="mock post", config=config, feature="COMPLIANCE_CHECK")
        sent = client.models.generate_content.call_args.kwargs["config"]
        self.assertEqual(sent.http_options.timeout, 20000)
        self.assertEqual(sent.http_options.retry_options.attempts, 1)
        self.assertEqual(sent.system_instruction, config.system_instruction)
        self.assertEqual(sent.response_schema, config.response_schema)
        self.assertEqual(config.http_options.timeout, 60000)

    async def test_failed_rulebook_model_is_not_called_again_by_context_or_visual_review(self):
        client = MagicMock()
        client.models.generate_content.side_effect = RuntimeError("503 UNAVAILABLE high demand")
        with patch.object(obs, "log_prompt"):
            with self.assertRaises(RuntimeError):
                await obs.generate(client, model="busy-model", contents="mock post", feature="COMPLIANCE_CHECK")
            for feature in ("CONTEXT_EVAL", "SOURCE_CHECK", "VISUAL_COMPLIANCE"):
                with self.assertRaisesRegex(RuntimeError, "temporarily unavailable"):
                    await obs.generate(client, model="busy-model", contents="mock post", feature=feature)
        self.assertEqual(client.models.generate_content.call_count, 1)

    async def test_timeout_cools_a_model_without_charging_another_network_attempt(self):
        client = MagicMock()
        client.models.generate_content.side_effect = TimeoutError("request timed out")
        with patch.object(obs, "log_prompt"):
            with self.assertRaises(TimeoutError):
                await obs.generate(client, model="slow-model", contents="mock post", feature="CONTEXT_EVAL")
            self.assertFalse(gemini.model_is_available("slow-model"))
            with self.assertRaises(RuntimeError):
                await obs.generate(client, model="slow-model", contents="mock post", feature="VISUAL_COMPLIANCE")
        self.assertEqual(client.models.generate_content.call_count, 1)

    async def test_context_reaches_configured_text_fallback_without_repeating_failed_models(self):
        gemini._model_cooldowns["busy-model"] = time.monotonic() + 60
        result = context._Result(id="0", status="VIOLATION", reasoning="reviewed", confidence=1)
        ask = AsyncMock(return_value=context._Response(results=[result]))
        with patch.object(context, "MODELS", ["busy-model"]), \
             patch.object(gemini, "get_gemini_fallback_models", return_value=["busy-model", "configured-text-fallback"]), \
             patch.object(context, "_item", return_value={"id": "0"}), patch.object(context, "_ask", ask):
            answers, model = await context._judge([(0, MagicMock())], None)
        self.assertEqual(set(answers), {0})
        self.assertEqual(model, "configured-text-fallback")
        ask.assert_awaited_once()
        self.assertEqual(ask.await_args.args[0], "configured-text-fallback")

    async def test_all_models_in_cooldown_do_not_start_a_second_sweep(self):
        gemini._model_cooldowns["busy-model"] = time.monotonic() + 60
        with patch.object(context, "MODELS", ["busy-model"]), \
             patch.object(gemini, "get_gemini_fallback_models", return_value=[]), \
             patch.object(context, "_ask", AsyncMock()) as ask, \
             patch.object(context.asyncio, "sleep", AsyncMock()) as sleep:
            answers, model = await context._judge([(0, MagicMock())], None)
        self.assertEqual(answers, {})
        self.assertIsNone(model)
        ask.assert_not_awaited()
        sleep.assert_not_awaited()

    def test_visual_review_has_its_own_bounded_image_request_timeout(self):
        config = policy.configure("VISUAL_COMPLIANCE", types.GenerateContentConfig(http_options=types.HttpOptions(timeout=60000)))
        self.assertEqual(config.http_options.timeout, 30000)
        self.assertEqual(config.http_options.retry_options.attempts, 1)

    def test_shorter_explicit_timeouts_are_retained(self):
        config = policy.configure("CONTEXT_EVAL", types.GenerateContentConfig(http_options=types.HttpOptions(timeout=5000)))
        self.assertEqual(config.http_options.timeout, 5000)

    def test_unrelated_features_keep_existing_retry_and_timeout_settings(self):
        config = types.GenerateContentConfig(http_options=types.HttpOptions(timeout=60000))
        self.assertIs(policy.configure("SENTIMENT", config), config)

    def test_text_fallbacks_retain_preferences_and_add_only_configured_models(self):
        with patch.object(gemini, "get_gemini_fallback_models", return_value=["preferred", "configured-extra"]):
            self.assertEqual(policy.text_models(["preferred"]), ["preferred", "configured-extra"])

    def test_provider_server_and_gateway_errors_rotate_and_share_a_cooldown(self):
        for code in (500, 504):
            error = RuntimeError(f"{code} BackendError")
            self.assertTrue(gemini.is_rotatable_model_error(error))
            gemini.record_model_failure(f"server-{code}", error)
            self.assertFalse(gemini.model_is_available(f"server-{code}"))

    async def test_source_comparison_remains_incomplete_when_all_models_are_cooling_down(self):
        from services.compliance.evaluation import source_check_service as source
        inventory = [{"claim": "Original factual passage", "where": "caption"}]
        for model_name in ("busy-source", "bedrock-test-profile"):
            with self.subTest(model=model_name):
                gemini._model_cooldowns[model_name] = time.monotonic() + 60
                with patch.object(source, "MODELS", [model_name]), \
                     patch.object(source.source_claim_inventory, "get", AsyncMock(return_value=inventory)), \
                     patch.object(source.source_passages, "excerpt", AsyncMock(return_value="Mock report")), \
                     patch.object(source._cache, "get", AsyncMock(return_value=None)), \
                     patch.object(source, "_ask", AsyncMock()) as ask, \
                     patch.object(source.asyncio, "sleep", AsyncMock()) as sleep:
                    claims, model = await source._compare("Original factual passage", "report", None, None, None, "Mock report")
                self.assertIsNone(claims)
                self.assertIsNone(model)
                ask.assert_not_awaited()
                sleep.assert_not_awaited()

    async def test_source_comparison_skips_cooled_profile_and_uses_available_profile(self):
        from services.compliance.evaluation import source_check_service as source
        gemini._model_cooldowns["busy-profile"] = time.monotonic() + 60
        inventory = [{"claim": "Original factual passage", "where": "caption"}]
        expected = source._Result(**inventory[0], verdict="SUPPORTED", evidence="Mock report", confidence=.95)
        ask = AsyncMock(return_value=source._Response(claims=[expected]))
        with patch.object(source, "MODELS", ["busy-profile", "available-profile"]), \
             patch.object(source.source_claim_inventory, "get", AsyncMock(return_value=inventory)), \
             patch.object(source.source_passages, "excerpt", AsyncMock(return_value="Mock report")), \
             patch.object(source._cache, "get", AsyncMock(return_value=None)), \
             patch.object(source._cache, "set", AsyncMock()), patch.object(source, "_ask", ask):
            claims, model = await source._compare("Original factual passage", "report", None, None, None, "Mock report")
        self.assertEqual(claims, [expected])
        self.assertEqual(model, "available-profile")
        ask.assert_awaited_once()
        self.assertEqual(ask.await_args.args[0], "available-profile")
