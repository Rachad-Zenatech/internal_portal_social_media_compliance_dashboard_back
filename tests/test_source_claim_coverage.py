"""Source-free posts still receive stable, grounded Rule 18 coverage."""
import unittest
from unittest.mock import AsyncMock, patch

from models.compliance.compliance_model import ComplianceCheckRequest, SourceCheck, SourceClaim
from services.compliance.evaluation import (
    compliance_service as cs, source_check_service as sc, source_claim_inventory as inventory,
    source_claim_coverage as coverage,
)

CLAIMS = ["Reliable cooling during transport", "Designed for demanding environments",
          "Supports temperature-sensitive supplies", "Portable refrigeration for mobile operations"]
POST = "A portable cooler for field teams.\nText in image 1:\nCoolBox\n" + "\n".join(CLAIMS)


class ClaimCoverageTests(unittest.IsolatedAsyncioTestCase):
    async def source_free(self, response):
        ask = AsyncMock(return_value=sc._Response(claims=[sc._Result(claim=c, verdict="UNCLEAR") for c in response]))
        write = AsyncMock(side_effect=lambda sql, key, claims: {"claims": claims})
        with (
            patch.object(sc.newsroom_facts, "material", AsyncMock(return_value=None)),
            patch.object(sc, "MODELS", ["test-model"]), patch.object(sc, "_ask", ask),
            patch.object(inventory.db, "fetch_one", AsyncMock(return_value=None)),
            patch.object(inventory.db, "execute", write),
        ):
            result = await sc.run(POST, include_image_citations=False)
        return result, ask

    async def test_image_capabilities_are_identified_without_any_source(self):
        result, ask = await self.source_free(CLAIMS)
        self.assertFalse(result.ai_failed)
        self.assertEqual(result.checks, [])  # No invented source in the source panel.
        self.assertEqual([f.passage for f in result.unbacked], CLAIMS)
        self.assertTrue(all(f.status == "EVIDENCE_REQUIRED" for f in result.unbacked))
        self.assertNotIn("<source>", ask.call_args.args[1])
        self.assertIn(POST, ask.call_args.args[1])
        costing = cs._charge_unsupported_claims(result.unbacked)
        self.assertEqual(sum(f.penalty for f in costing), cs.EVIDENCE_CAP)
        self.assertTrue(all(f.penalty <= cs.EVIDENCE_PENALTY for f in result.unbacked))

    async def test_newsroom_silence_does_not_erase_image_claims(self):
        newsroom = SourceCheck(reference="Company newsroom", kind="URL", where="newsroom", status="READ")
        answers = [sc._Result(claim=c, where="image", verdict="NOT_FOUND", confidence=0.9) for c in CLAIMS]
        with (
            patch.object(sc.newsroom_facts, "material", AsyncMock(return_value=(newsroom, ("Newsroom", "No cooler evidence.")))),
            patch.object(sc, "_compare", AsyncMock(return_value=(answers, "test-model"))),
            patch.object(sc.source_passages, "EMBED_ENABLED", False),
            patch.object(coverage, "identify", AsyncMock()) as identify,
        ):
            result = await sc.run(POST, include_image_citations=False)
        identify.assert_not_awaited()
        self.assertFalse(result.ai_failed)
        self.assertEqual(result.checks, [])  # Keep irrelevant automatic newsroom summaries out of the panel.
        self.assertEqual([finding.passage for finding in result.unbacked], CLAIMS)
        self.assertTrue(all(f.status == "EVIDENCE_REQUIRED" for f in result.unbacked))

    async def test_inventory_failure_marks_incomplete_instead_of_clean(self):
        result = sc.SourceCheckResult()
        with patch.object(coverage, "identify", AsyncMock(return_value=None)):
            await coverage.complete(result, None, POST, None, [], 12)
        self.assertTrue(result.ai_failed)
        self.assertEqual(result.unbacked, [])

    async def test_successful_empty_inventory_is_not_treated_as_failure(self):
        result, _ = await self.source_free([])
        self.assertFalse(result.ai_failed)
        self.assertEqual(result.unbacked, [])

    async def test_existing_comparisons_reuse_their_fixed_inventory(self):
        result = sc.SourceCheckResult(checks=[SourceCheck(reference="Evidence", kind="DOCUMENT",
            where="attached", status="READ", claims=[SourceClaim(claim=CLAIMS[0], where="image", verdict="SUPPORTED")])])
        saved = [{"claim": claim, "where": "image"} for claim in CLAIMS]
        with patch.object(coverage, "identify", AsyncMock()) as identify:
            await coverage.complete(result, saved, POST, None, [], 12)
        identify.assert_not_awaited()
        self.assertEqual([f.passage for f in result.unbacked], CLAIMS[1:])

    async def test_no_duplicate_missing_evidence_when_a_source_already_could_not_support_it(self):
        for verdict in ("SUPPORTED", "NOT_FOUND", "UNCLEAR", "CONTRADICTED"):
            with self.subTest(verdict=verdict):
                checks = [SourceCheck(reference="Evidence", kind="DOCUMENT", where="attached", status="READ",
                    claims=[SourceClaim(claim=CLAIMS[0], where="image", verdict=verdict)])]
                self.assertEqual(coverage.missing_findings([{"claim": CLAIMS[0], "where": "image"}], checks), [])

    async def test_pipeline_includes_source_free_claims_in_score(self):
        saved = [{"claim": claim, "where": "image"} for claim in CLAIMS]
        source_result = sc.SourceCheckResult(unbacked=coverage.missing_findings(saved, []))
        with (
            patch.object(cs, "applicable_rules", AsyncMock(return_value=[])),
            patch.object(cs, "_semantic_findings", AsyncMock(return_value=[])),
            patch.object(sc, "run", AsyncMock(return_value=source_result)),
            patch.object(cs, "rulebook_version", AsyncMock(return_value="test")),
        ):
            result = await cs.check_compliance(ComplianceCheckRequest(text=POST))
        self.assertFalse(result.ai_unavailable)
        self.assertEqual(result.score, 100 - cs.EVIDENCE_CAP)
        self.assertEqual(result.evidence_count, len(CLAIMS))
        self.assertFalse(result.is_compliant)
