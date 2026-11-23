"""Model rotation is exercised offline, without sending any image or prompt to Gemini."""
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from services.compliance.media import image_text_service as images


class ImageModelFallbackTests(unittest.IsolatedAsyncioTestCase):
    async def call(self, side_effect, models=None):
        generate = AsyncMock(side_effect=side_effect)
        with patch.object(images, "_get_client", return_value=object()), \
             patch.object(images.ai_observability, "generate", generate):
            result = await images._generate(["mock-image"], feature="VISUAL_COMPLIANCE", models=models)
        return result, generate

    async def test_all_busy_models_fall_through_to_the_fourth_model(self):
        models = ["first", "second", "third", "fourth"]
        answer = SimpleNamespace(text="valid visual result")
        result, generate = await self.call([RuntimeError("503 high demand"), TimeoutError(),
                                           RuntimeError("429 RESOURCE_EXHAUSTED"), answer], models)
        self.assertIs(result, answer)
        self.assertEqual([c.kwargs["model"] for c in generate.await_args_list], models)
        self.assertTrue(all(c.kwargs["feature"] == "VISUAL_COMPLIANCE" for c in generate.await_args_list))

    async def test_a_success_stops_fallback(self):
        answer = SimpleNamespace(text="result")
        _, generate = await self.call([answer], ["first", "second"])
        self.assertEqual(generate.await_count, 1)

    async def test_every_configured_model_is_tried_before_unavailable(self):
        generate = AsyncMock(side_effect=RuntimeError("503 UNAVAILABLE"))
        with patch.object(images, "_get_client", return_value=object()), \
             patch.object(images.ai_observability, "generate", generate):
            with self.assertRaisesRegex(RuntimeError, "No image-text model"):
                await images._generate([], feature="VISUAL_COMPLIANCE", models=["one", "two", "three", "four"])
        self.assertEqual(generate.await_count, 4)

    async def test_ocr_callers_keep_their_own_model_chain(self):
        answer = SimpleNamespace(text="OCR result")
        with patch.object(images, "MODELS", ["ocr-one", "ocr-two"]):
            _, generate = await self.call([RuntimeError("503 UNAVAILABLE"), answer])
        self.assertEqual([c.kwargs["model"] for c in generate.await_args_list], ["ocr-one", "ocr-two"])

    async def test_unrelated_errors_do_not_rotate_or_repeat_the_request(self):
        generate = AsyncMock(side_effect=ValueError("Invalid local image input"))
        with patch.object(images, "_get_client", return_value=object()), \
             patch.object(images.ai_observability, "generate", generate):
            with self.assertRaisesRegex(ValueError, "Invalid local image"):
                await images._generate([], models=["one", "two"])
        self.assertEqual(generate.await_count, 1)

    async def test_duplicate_models_do_not_repeat_calls(self):
        answer = SimpleNamespace(text="result")
        _, generate = await self.call([RuntimeError("503 UNAVAILABLE"), answer], ["one", "one", "two"])
        self.assertEqual([c.kwargs["model"] for c in generate.await_args_list], ["one", "two"])
