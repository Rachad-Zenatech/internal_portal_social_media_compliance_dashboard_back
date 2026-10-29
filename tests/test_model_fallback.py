import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from google.genai import errors

from models.sentiment_model import SentimentResult
from services.compliance import image_text_service
from services.compliance import sentiment_service as s

SS = "services.compliance.sentiment_service"


def result():
    return SentimentResult.model_validate(
        {
            "summary": "s",
            "overall_assessment": "NO_CONCERNS",
            "reputational_concern": "NONE",
            "tone": {"primary": "Informative", "intensity": "LOW", "evidence": []},
            "brand_alignment": {"rating": "ALIGNED", "explanation": "e"},
            "controversy": {"applies": False},
        }
    )


def api_error(code, message):
    return errors.APIError(code, {"error": {"code": code, "message": message, "status": "x"}})


class SentimentModelChainTests(unittest.IsolatedAsyncioTestCase):
    CHAIN = ["model-a", "model-b", "model-c"]

    async def generate(self, outcomes, inp=None, fallback=True):
        """Run the chain where each model either answers or raises; returns (result or error, models tried)."""
        tried = []

        async def generate_with(client, model, *args):
            tried.append(model)
            outcome = outcomes[model]
            if isinstance(outcome, Exception):
                raise outcome
            return outcome

        with (
            patch(f"{SS}.MODEL_CHAIN", self.CHAIN),
            patch(f"{SS}.MODEL", "model-a"),
            patch(f"{SS}._generate_with", new=AsyncMock(side_effect=generate_with)),
        ):
            try:
                out = await s._generate(MagicMock(), inp or {}, None, fallback=fallback)
            except Exception as e:
                out = e
        return out, tried

    async def test_the_first_model_is_used_when_it_answers(self):
        out, tried = await self.generate({"model-a": result()})
        self.assertEqual(tried, ["model-a"])
        self.assertEqual(out.model_used, "model-a")
        self.assertIsNone(out.model_note)

    async def test_a_used_up_daily_limit_moves_to_the_next_model_and_says_so(self):
        out, tried = await self.generate({"model-a": s.QuotaExhausted("model-a"), "model-b": result()})
        self.assertEqual(tried, ["model-a", "model-b"])
        self.assertEqual(out.model_used, "model-b")
        self.assertIn("model-a's daily limit was used up", out.model_note)

    async def test_a_retired_model_is_skipped(self):
        out, _ = await self.generate({"model-a": s.ModelUnavailable("model-a", "isn't available"), "model-b": result()})
        self.assertEqual(out.model_used, "model-b")
        self.assertIn("model-a isn't available", out.model_note)

    async def test_a_model_that_stays_overloaded_is_skipped(self):
        out, _ = await self.generate({"model-a": s.ModelUnavailable("model-a", "was too busy"), "model-b": result()})
        self.assertEqual(out.model_used, "model-b")
        self.assertIn("model-a was too busy", out.model_note)

    async def test_when_every_limit_is_used_up_the_run_can_be_scheduled(self):
        out, tried = await self.generate({m: s.QuotaExhausted(m) for m in self.CHAIN})
        self.assertIsInstance(out, s.QuotaExhausted)
        self.assertEqual(tried, self.CHAIN)
        self.assertIsNotNone(out.reset_at)

    async def test_an_inputs_saved_model_goes_first_then_the_chain(self):
        # e.g. an analysis queued for the retired gemini-2.5-pro still runs, on the chain.
        out, tried = await self.generate(
            {"gemini-2.5-pro": s.ModelUnavailable("gemini-2.5-pro", "isn't available"), "model-a": result()},
            inp={"model": "gemini-2.5-pro"},
        )
        self.assertEqual(tried, ["gemini-2.5-pro", "model-a"])
        self.assertEqual(out.model_used, "model-a")

    async def test_testing_one_model_never_falls_back(self):
        out, tried = await self.generate({"model-a": s.QuotaExhausted("model-a"), "model-b": result()}, fallback=False)
        self.assertEqual(tried, ["model-a"])
        self.assertIsInstance(out, s.QuotaExhausted)


class SentimentErrorClassificationTests(unittest.IsolatedAsyncioTestCase):
    async def run_one(self, error):
        client = MagicMock()
        client.models.generate_content = MagicMock(side_effect=error)
        await self.run_one_with(client)

    async def run_one_with(self, client):
        inp = {"title": "t", "caption": "c", "transcript": None, "guidelines": [], "audience": None, "brand": None,
               "region": None, "platforms": [], "rule_sets": [], "channel": None, "is_ceo_channel": False,
               "controversy_dial": False}
        with patch(f"{SS}.asyncio.sleep", new=AsyncMock()):
            await s._generate_with(client, "model-a", inp, None)

    async def test_a_daily_quota_error_is_reported_as_quota(self):
        with self.assertRaises(s.QuotaExhausted):
            await self.run_one(api_error(429, "Quota exceeded for metric GenerateRequestsPerDayPerProjectPerModel-FreeTier"))

    async def test_a_retired_model_fails_fast_without_retrying(self):
        error = api_error(404, "This model models/gemini-2.5-pro is no longer available to new users.")
        with self.assertRaises(s.ModelUnavailable) as caught:
            await self.run_one(error)
        self.assertEqual(caught.exception.reason, "isn't available")

    async def test_a_timed_out_call_hands_over_without_waiting_again(self):
        import httpx

        client = MagicMock()
        client.models.generate_content = MagicMock(side_effect=httpx.ReadTimeout("timed out"))
        with self.assertRaises(s.ModelUnavailable) as caught:
            await self.run_one_with(client)
        self.assertEqual(caught.exception.reason, "timed out")
        self.assertEqual(client.models.generate_content.call_count, 1)

    async def test_each_call_has_a_time_limit(self):
        client = MagicMock()
        client.models.generate_content = MagicMock(side_effect=api_error(503, "high demand"))
        with self.assertRaises(s.ModelUnavailable):
            await self.run_one_with(client)
        config = client.models.generate_content.call_args.kwargs["config"]
        self.assertEqual(config.http_options.timeout, s.GENERATE_TIMEOUT_SEC * 1000)

    async def test_a_model_still_overloaded_after_retries_hands_over(self):
        with self.assertRaises(s.ModelUnavailable) as caught:
            await self.run_one(api_error(503, "This model is currently experiencing high demand."))
        self.assertEqual(caught.exception.reason, "was too busy")


class ImageTextModelChainTests(unittest.IsolatedAsyncioTestCase):
    async def test_the_next_light_model_is_tried_when_one_is_out_of_quota(self):
        answer = SimpleNamespace(text='{"text": "Hello"}')
        client = MagicMock()
        client.models.generate_content = MagicMock(side_effect=[api_error(429, "RESOURCE_EXHAUSTED"), answer])
        with (
            patch.object(image_text_service, "MODELS", ["lite-a", "lite-b"]),
            patch.object(image_text_service, "_get_client", return_value=client),
        ):
            response = await image_text_service._generate(contents=["x"])

        self.assertIs(response, answer)
        models_tried = [call.kwargs["model"] for call in client.models.generate_content.call_args_list]
        self.assertEqual(models_tried, ["lite-a", "lite-b"])


if __name__ == "__main__":
    unittest.main()
