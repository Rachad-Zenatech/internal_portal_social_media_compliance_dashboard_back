"""Stable comparisons and proportionate mismatch penalties; no provider or network calls."""
import json
import unittest
from unittest.mock import AsyncMock, patch

from services.compliance.evaluation import source_check_service as sc, source_comparison_cache as cache


class ProportionateMatchingTests(unittest.TestCase):
    def test_uncertain_marketing_overbreadth_is_not_a_confirmed_contradiction(self):
        answer = sc._Result(claim="The cooler provides portable cold storage wherever the mission goes.",
            verdict="CONTRADICTED", evidence="A portable fridge for transport and delivery.", confidence=0.7,
            contradiction_kind="MATERIAL_OVERSTATEMENT", note="The source does not list every mission.")
        checked = sc._to_claims([answer], answer.evidence)[0]
        self.assertEqual(checked.verdict, "UNCLEAR")

    def test_missing_coverage_is_not_a_conflict_even_at_high_confidence(self):
        answer = sc._Result(claim="Rated for -40 C", verdict="CONTRADICTED", confidence=0.99,
            contradiction_kind="NOT_STATED", evidence="A portable fridge.")
        self.assertEqual(sc._to_claims([answer], answer.evidence)[0].verdict, "NOT_FOUND")

    def test_explicit_conflicting_number_with_direct_source_quote_remains_contradicted(self):
        answer = sc._Result(claim="The battery lasts 20 hours", verdict="CONTRADICTED", confidence=0.95,
            contradiction_kind="EXPLICIT_CONFLICT", evidence="The battery lasts 10 hours.")
        self.assertEqual(sc._to_claims([answer], answer.evidence)[0].verdict, "CONTRADICTED")

    def test_unfounded_conflict_evidence_never_receives_a_contradiction_verdict(self):
        answer = sc._Result(claim="The battery lasts 20 hours", verdict="CONTRADICTED", confidence=0.99,
            contradiction_kind="EXPLICIT_CONFLICT", evidence="The battery lasts 10 hours.")
        self.assertEqual(sc._to_claims([answer], "A portable fridge.")[0].verdict, "UNCLEAR")

    def test_temperature_sensitive_supplies_can_be_supported_by_medical_examples(self):
        quote = "It transports vaccines and blood components requiring precise thermoregulation."
        answer = sc._Result(claim="Supports temperature-sensitive supplies", where="image", verdict="SUPPORTED",
            confidence=0.95, evidence=quote)
        self.assertEqual(sc._to_claims([answer], quote)[0].verdict, "SUPPORTED")


class SharedComparisonTests(unittest.IsolatedAsyncioTestCase):
    async def test_unchanged_evidence_reuses_first_answer_after_worker_restart_and_manual_rerun(self):
        stored = {}
        async def read(sql, key):
            return {"result": stored[key]} if key in stored else None
        async def write(sql, key, payload):
            stored.setdefault(key, payload)
            return {"result": stored[key]}
        ask = AsyncMock(return_value=sc._Response(claims=[
            sc._Result(claim="Capacity is 10 litres", where="caption", verdict="SUPPORTED", confidence=0.95,
                       evidence="Capacity is 10 litres")]))
        await sc._cache.clear()
        with (
            patch.object(sc.source_claim_coverage, "identify", AsyncMock(return_value=[{"claim":"Capacity is 10 litres", "where":"caption"}])),
            patch.object(sc, "MODELS", ["test-profile"]), patch.object(sc, "_ask", ask),
            patch.object(sc.source_passages, "excerpt", AsyncMock(return_value="Capacity is 10 litres")),
            patch.object(cache.db, "fetch_one", AsyncMock(side_effect=read)),
            patch.object(cache.db, "execute", AsyncMock(side_effect=write)),
        ):
            first = await sc._compare("Capacity is 10 litres", "Evidence", None, "https://example.com/spec", None, "Capacity is 10 litres")
            await sc._cache.clear()  # Simulate a different API worker/person.
            token = sc.source_recheck.refresh.set(True)
            try:
                second = await sc._compare("Capacity is 10 litres", "Evidence", None, "https://example.com/spec", None, "Capacity is 10 litres")
            finally:
                sc.source_recheck.refresh.reset(token)
            self.assertEqual(first, second)
            self.assertEqual(ask.await_count, 1)
            await sc._compare("Capacity is 10 litres", "Evidence", None, "https://example.com/spec", None, "Capacity is 12 litres")
            self.assertEqual(ask.await_count, 2)

    async def test_omitted_claim_answer_is_not_shared_as_a_completed_comparison(self):
        with (
            patch.object(sc.source_claim_coverage, "identify", AsyncMock(return_value=[{"claim":"Capacity is 10 litres", "where":"caption"}])),
            patch.object(sc, "MODELS", ["test-profile"]),
            patch.object(sc, "_ask", AsyncMock(return_value=sc._Response(claims=[]))),
            patch.object(sc.source_passages, "excerpt", AsyncMock(return_value="Capacity is 10 litres")),
            patch.object(cache, "get", AsyncMock(return_value=None)),
            patch.object(cache, "put", AsyncMock()) as write,
        ):
            await sc._cache.clear()
            result, _ = await sc._compare("Capacity is 10 litres", "Evidence", None, None, None, "Capacity is 10 litres")
        self.assertFalse(result.complete)
        write.assert_not_awaited()

    async def test_first_committed_answer_wins_concurrent_writes(self):
        first = {"claims": [{"claim":"Capacity is 10 litres", "verdict":"SUPPORTED", "confidence":0.9}], "model":"first"}
        second = ([sc._Result(claim="Capacity is 10 litres", verdict="UNCLEAR")], "second")
        with patch.object(cache.db, "execute", AsyncMock(return_value={"result": json.dumps(first)})):
            result = await cache.put("key", second)
        self.assertEqual(result[1], "first")
        self.assertEqual(result[0][0].verdict, "SUPPORTED")
