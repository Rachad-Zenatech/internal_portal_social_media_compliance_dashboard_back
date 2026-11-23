"""Recovery from temporary model overload without retry storms or changed review semantics."""

import asyncio
import unittest
from contextlib import ExitStack
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from services.compliance.media import image_text_service, model_backoff


class ModelError(Exception):
    def __init__(self, code, message="Provider error"):
        super().__init__(message)
        self.code = code


class MediaModelBackoffTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.clock = 0.
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.stack.enter_context(patch.object(model_backoff.time, "monotonic", side_effect=lambda: self.clock))
        self.stack.enter_context(patch.object(model_backoff.random, "uniform", return_value=0.))
        self.sleep = self.stack.enter_context(patch.object(model_backoff.asyncio, "sleep", AsyncMock(side_effect=self.advance)))

    async def advance(self, seconds):
        self.clock += seconds

    async def test_immediate_fallback_succeeds_without_sleeping(self):
        request = AsyncMock(side_effect=[ModelError(503, "High demand"), "success"])
        result = await model_backoff.generate(["primary", "fallback"], request, timeout_sec=60)
        self.assertEqual(result, "success")
        self.assertEqual([c.args[0] for c in request.await_args_list], ["primary", "fallback"])
        self.sleep.assert_not_awaited()

    async def test_overload_recovers_after_backoff_and_tries_all_models_first(self):
        request = AsyncMock(side_effect=[ModelError(503), ModelError(503), "recovered"])
        self.assertEqual(await model_backoff.generate(["a", "b"], request, timeout_sec=60), "recovered")
        self.assertEqual([c.args[0] for c in request.await_args_list], ["a", "b", "a"])
        self.sleep.assert_awaited_once_with(1.)
        self.assertEqual(request.await_args_list[-1].args[2], 2)

    async def test_persistent_outage_stops_after_two_retry_rounds(self):
        request = AsyncMock(side_effect=ModelError(503))
        with self.assertRaisesRegex(RuntimeError, "No image-text model") as raised:
            await model_backoff.generate(["a", "b"], request, timeout_sec=60)
        self.assertEqual(request.await_count, 6)
        self.assertEqual([c.args[0] for c in self.sleep.await_args_list], [1., 2.])
        self.assertIsInstance(raised.exception.__cause__, ModelError)

    async def test_quota_missing_models_and_timeouts_are_not_retried(self):
        for error in (ModelError(429, "Quota exceeded"), ModelError(404, "Model not found"), TimeoutError()):
            request = AsyncMock(side_effect=error)
            with self.assertRaises(RuntimeError):
                await model_backoff.generate(["a", "b"], request, timeout_sec=60)
            self.assertEqual(request.await_count, 2)
        self.sleep.assert_not_awaited()

    async def test_invalid_key_and_programming_errors_propagate(self):
        for error in (ModelError(403, "Invalid API key"), ValueError("Bad request")):
            request = AsyncMock(side_effect=error)
            with self.assertRaises(type(error)):
                await model_backoff.generate(["a", "b"], request, timeout_sec=60)
            self.assertEqual(request.await_count, 1)
        self.sleep.assert_not_awaited()

    async def test_only_temporarily_unavailable_models_are_retried(self):
        request = AsyncMock(side_effect=[ModelError(404), ModelError(503), "success"])
        self.assertEqual(await model_backoff.generate(["gone", "busy"], request, timeout_sec=60), "success")
        self.assertEqual([c.args[0] for c in request.await_args_list], ["gone", "busy", "busy"])

    async def test_retry_budget_limits_http_timeout_and_stops_further_calls(self):
        async def attempt(model, timeout, number):
            if number == 1:
                raise ModelError(503)
            self.assertEqual(timeout, 3.)
            self.clock += timeout
            raise TimeoutError()

        request = AsyncMock(side_effect=attempt)
        with self.assertRaises(RuntimeError):
            await model_backoff.generate(["a", "b"], request, timeout_sec=60, retry_budget_sec=4)
        self.assertEqual(request.await_count, 3)
        self.assertEqual(self.clock, 4.)

    async def test_retry_can_be_disabled_or_skipped_when_budget_is_too_small(self):
        for options in ({"retry_rounds": 0}, {"retry_budget_sec": .5}):
            request = AsyncMock(side_effect=ModelError(503))
            with self.assertRaises(RuntimeError):
                await model_backoff.generate(["a", "b"], request, timeout_sec=60, **options)
            self.assertEqual(request.await_count, 2)
        self.sleep.assert_not_awaited()

    async def test_cancellation_is_propagated(self):
        request = AsyncMock(side_effect=asyncio.CancelledError())
        with self.assertRaises(asyncio.CancelledError):
            await model_backoff.generate(["a", "b"], request, timeout_sec=60)
        self.assertEqual(request.await_count, 1)


class MediaGenerationIntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def test_visual_review_keeps_schema_feature_and_contents_through_retry(self):
        response = SimpleNamespace(text="valid assessment")
        generate = AsyncMock(side_effect=[ModelError(503), ModelError(503), response])
        contents, schema = ["prompt", object()], {"type": "OBJECT"}
        with (
            patch.object(image_text_service, "_get_client", return_value=object()),
            patch.object(image_text_service, "MODELS", ["a", "b"]),
            patch.object(image_text_service, "RETRY_ROUNDS", 2),
            patch.object(image_text_service, "RETRY_BUDGET_SEC", 20.),
            patch.object(image_text_service.ai_observability, "generate", generate),
            patch.object(model_backoff.asyncio, "sleep", AsyncMock()),
        ):
            self.assertIs(await image_text_service._generate(contents, "VISUAL_COMPLIANCE", schema), response)
        for call in generate.await_args_list:
            self.assertIs(call.kwargs["contents"], contents)
            self.assertEqual(call.kwargs["feature"], "VISUAL_COMPLIANCE")
            self.assertEqual(call.kwargs["config"].response_schema, schema)
            self.assertEqual(call.kwargs["config"].http_options.retry_options.attempts, 1)
        self.assertLess(generate.await_args_list[-1].kwargs["config"].http_options.timeout, 60000)
