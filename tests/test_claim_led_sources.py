"""Claim-led evidence: no Cartesian source checking, lost product support, or incidental OCR claims."""
import json
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from models.compliance.compliance_model import SourceCheck, SourceClaim, ProvidedSource
from services.compliance.evaluation import (
    source_check_service as sc, source_claim_routing as routing, source_claim_coverage as coverage,
    source_claim_inventory as inventory, source_claim_policy as policy, newsroom_claim_sources as news,
)


def item(text, where="caption"):
    return {"claim": text, "where": where}


def source(name, body, where="attached"):
    return SourceCheck(reference=name, kind="DOCUMENT", where=where, status="READ"), (name, body)


def answer(claim, verdict="SUPPORTED"):
    return SourceClaim(**claim, verdict=verdict, confidence=.95, evidence=claim["claim"])


class ClaimRoutingTests(unittest.IsolatedAsyncioTestCase):
    async def test_two_claims_use_their_relevant_sources_once(self):
        claims = [item("Drone payload is 50 kg"), item("Battery lasts 10 hours")]
        sources = [source("payload", "Drone payload is 50 kg."), source("battery", "Battery lasts 10 hours.")]
        calls = []
        async def compare(check, material, assigned):
            calls.append((check.reference, assigned))
            return [answer(claim) for claim in assigned]
        with patch.object(routing.source_passages, "rank", AsyncMock(return_value=[[1., 0.], [0., 1.]])):
            failures = await routing.verify(claims, sources, compare)
        self.assertEqual(failures, set())
        self.assertEqual(calls, [("payload", claims[:1]), ("battery", claims[1:])])

    async def test_fallback_is_only_for_an_unsettled_claim_and_stops_at_support(self):
        claims = [item("Drone payload is 50 kg")]
        sources = [source(str(i), "Payload details.") for i in range(3)]
        calls = []
        async def compare(check, material, assigned):
            calls.append(check.reference)
            return [answer(assigned[0], "NOT_FOUND" if check.reference == "0" else "SUPPORTED")]
        with patch.object(routing.source_passages, "rank", AsyncMock(return_value=[[.9, .8, .7]])):
            self.assertEqual(await routing.verify(claims, sources, compare), set())
        self.assertEqual(calls, ["0", "1"])

    async def test_failed_primary_is_recovered_by_relevant_fallback(self):
        claims = [item("Drone payload is 50 kg")]
        compare = AsyncMock(side_effect=[RuntimeError("temporary"), [answer(claims[0])]])
        with patch.object(routing.source_passages, "rank", AsyncMock(return_value=[[1., .8]])):
            failures = await routing.verify(claims, [source("one", "payload"), source("two", "payload")], compare)
        self.assertFalse(failures)

    async def test_failed_checks_are_incomplete_not_missing_evidence_deductions(self):
        claims = [item("Drone payload is 50 kg")]
        with patch.object(routing.source_passages, "rank", AsyncMock(return_value=[[1.]])):
            failed = await routing.verify(claims, [source("one", "payload")], AsyncMock(return_value=None))
        self.assertEqual(coverage.missing_findings(claims, [], failed=failed), [])
        self.assertEqual(len(coverage.missing_findings(claims, [])), 1)

    async def test_specific_product_facts_are_retrieved_per_claim(self):
        rows = [dict(title=f"Defense event {i}", body="Drone government defense event announcement", url=f"https://example.com/event/{i}") for i in range(10)]
        rows += [dict(title="IQ Nano inventory trial", body="IQ Nano supports warehouse inventory management", url="https://example.com/nano"),
                 dict(title="IQ Aqua underwater testing", body="IQ Aqua is an underwater drone in field testing", url="https://example.com/aqua")]
        with patch.object(news, "fetch_all", AsyncMock(return_value=rows)), \
             patch.object(news.source_passages, "_embeddings", AsyncMock(return_value=None)):
            reads = await news.readings([item("our IQ Nano supporting inventory management"), item("the IQ Aqua underwater drone")])
        urls = {check.url for check, _ in reads}
        self.assertIn("https://example.com/nano", urls)
        self.assertIn("https://example.com/aqua", urls)
        self.assertNotIn("https://example.com/event/0", urls)

    async def test_extraction_omits_incidental_signage_without_losing_soft_claims(self):
        text = "IQ Nano supports inventory management.\nText in image 1:\nTECH\nzenatech.com\n2024 SPONSORS"
        response = SimpleNamespace(claims=[
            sc._Result(claim="IQ Nano supports inventory management", where="caption", verdict="UNCLEAR"),
            sc._Result(claim="TECH\nzenatech.com\n2024 SPONSORS", where="image", verdict="UNCLEAR")])
        with patch.object(inventory, "screen_input", return_value=SimpleNamespace(verdict="ALLOW")):
            claims = await inventory.extract(text, AsyncMock(return_value=response), ["mock"], 12)
        self.assertEqual(claims, [item("IQ Nano supports inventory management")])
        self.assertNotEqual(inventory.VERSION, "claims-v5-partial-substantiation")

    async def test_full_run_passes_only_assigned_inventory_to_each_source(self):
        claims = [item("Drone payload is 50 kg"), item("Battery lasts 10 hours")]
        calls = []
        async def compare(text, label, title, url, published, source_text, *, inventory):
            calls.append((label, inventory))
            return [sc._Result(**claim, verdict="SUPPORTED", evidence=claim["claim"], confidence=.95) for claim in inventory], "mock"
        async def checked(results, source_text):
            return [SourceClaim(claim=r.claim, where=r.where, verdict=r.verdict, evidence=r.evidence, confidence=r.confidence) for r in results]
        with patch.object(coverage, "identify", AsyncMock(return_value=claims)), \
             patch.object(news, "readings", AsyncMock(return_value=[])), \
             patch.object(sc.citation_finder, "find", return_value=[]), \
             patch.object(sc, "_compare", compare), \
             patch.object(sc.source_passages, "checked_claims", checked), \
             patch.object(routing.source_passages, "rank", AsyncMock(return_value=[[1., 0.], [0., 1.]])):
            result = await sc.run("Drone payload is 50 kg. Battery lasts 10 hours.", [
                ProvidedSource(name="Payload", text="Drone payload is 50 kg."),
                ProvidedSource(name="Battery", text="Battery lasts 10 hours.")])
        self.assertFalse(result.ai_failed)
        self.assertEqual(result.unbacked, [])
        self.assertEqual([assigned for _, assigned in calls], [claims[:1], claims[1:]])
        self.assertEqual([len(check.claims) for check in result.checks], [1, 1])

    async def test_long_source_passages_are_selected_for_assigned_claims_only(self):
        assigned = [item("Payload is 50 kg")]
        response = sc._Response(claims=[sc._Result(**assigned[0], verdict="SUPPORTED", evidence="Payload is 50 kg", confidence=.95)])
        with patch.object(sc.source_comparison_cache, "get", AsyncMock(return_value=None)), \
             patch.object(sc.source_comparison_cache, "put", AsyncMock(side_effect=lambda key, value: value)), \
             patch.object(sc._cache, "get", AsyncMock(return_value=None)), \
             patch.object(sc.source_passages, "excerpt", AsyncMock(return_value="Payload is 50 kg")) as excerpt, \
             patch.object(sc, "_ask", AsyncMock(return_value=response)), \
             patch.object(sc, "MODELS", ["mock"]), \
             patch.object(sc, "model_is_available", return_value=True), \
             patch.object(sc, "screen_input", return_value=SimpleNamespace(verdict="ALLOW")):
            await sc._compare("Payload is 50 kg. Battery lasts 10 hours.", "Source", None, None, None,
                              "Payload is 50 kg", inventory=assigned)
        self.assertEqual(excerpt.await_args.args[0], "Payload is 50 kg")

    async def test_newsroom_lookup_failure_does_not_block_fully_supported_attached_claims(self):
        claims = [item("Payload is 50 kg")]
        async def checked(results, source_text):
            return [answer(claims[0])]
        with patch.object(coverage, "identify", AsyncMock(return_value=claims)), \
             patch.object(news, "readings", AsyncMock(side_effect=RuntimeError("temporary"))), \
             patch.object(sc.citation_finder, "find", return_value=[]), \
             patch.object(sc, "_compare", AsyncMock(return_value=([sc._Result(**claims[0], verdict="SUPPORTED")], "mock"))), \
             patch.object(sc.source_passages, "checked_claims", checked), \
             patch.object(routing.source_passages, "rank", AsyncMock(return_value=[[1.]])):
            result = await sc.run("Payload is 50 kg", [ProvidedSource(name="Spec", text="Payload is 50 kg")])
        self.assertFalse(result.ai_failed)
        self.assertEqual(result.unbacked, [])

    async def test_newsroom_lookup_failure_without_other_evidence_is_incomplete_not_a_deduction(self):
        with patch.object(coverage, "identify", AsyncMock(return_value=[item("Payload is 50 kg")])), \
             patch.object(news, "readings", AsyncMock(side_effect=RuntimeError("temporary"))), \
             patch.object(sc.citation_finder, "find", return_value=[]):
            result = await sc.run("Payload is 50 kg")
        self.assertTrue(result.ai_failed)
        self.assertEqual(result.unbacked, [])


class IncidentalPolicyTests(unittest.TestCase):
    def test_background_metadata_is_not_an_authored_assertion(self):
        self.assertTrue(policy.incidental("TECH\nzenatech.com\n2024 SPONSORS", "image"))
        self.assertFalse(policy.incidental("ZenaTech sponsored the 2024 symposium", "image"))
        self.assertFalse(policy.incidental("Supports temperature-sensitive supplies", "image"))
        self.assertFalse(policy.incidental("IQ Nano supports inventory management", "caption"))
