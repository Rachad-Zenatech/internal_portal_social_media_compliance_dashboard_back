"""Formatting repair preserves original evidence and cannot hide changed facts or incomplete reviews."""
import unittest
from unittest.mock import AsyncMock, patch

from models.compliance.compliance_model import ComplianceCheckRequest
from services.compliance.evaluation import (
    compliance_service as cs, source_claim_grounding as grounding,
    source_claim_inventory as inventory, source_check_service as sc,
)


class ClaimGroundingTests(unittest.TestCase):
    def test_sentence_spacing_recovers_original_excerpt(self):
        original = "Zero.That is how many government contracts a company needs."
        self.assertEqual(grounding.excerpts(original, original.replace("Zero.That", "Zero. That")),
                         [(original, "caption")])

    def test_list_separators_recover_original_image_text(self):
        original = "RANGES OPENED TO PRIVATE INDUSTRY\nDugway Proving Ground, Utah\nWest Cibola Range, Arizona"
        claim = "RANGES OPENED TO PRIVATE INDUSTRY: Dugway Proving Ground, Utah; West Cibola Range, Arizona"
        self.assertEqual(grounding.excerpts("Text in image 1: " + original, claim), [(original, "image")])

    def test_changed_numbers_signs_words_qualifiers_and_order_are_rejected(self):
        pairs = [("Range is 3.0 km", "Range is 30 km"), ("Temperature is -5 C", "Temperature is 5 C"),
                 ("Capacity is 1,000 units", "Capacity is 1000 units"),
                 ("It can not fly", "It can fly"), ("Up to 30 km", "Up to 100 km"),
                 ("Supports cooling", "Guarantees cooling"), ("A supports B", "B supports A")]
        for original, claim in pairs:
            with self.subTest(claim=claim):
                self.assertEqual(grounding.excerpts(original, claim), [])

    def test_matching_cannot_join_different_images_or_caption_with_image(self):
        for post in ("Range is Text in image 1: 30 km", "Text in image 1: Range is Text in image 2: 30 km"):
            self.assertEqual(grounding.excerpts(post, "Range is 30 km"), [])

    def test_long_original_is_not_truncated_to_make_it_fit(self):
        self.assertEqual(grounding.excerpts("Range" + " " * 400 + "is 30 km", "Range is 30 km"), [])

    def test_verification_keeps_supported_verdict_with_original_wording_after_formatting(self):
        original = "Zero.That is how many contracts are needed."
        saved = [{"claim": original, "where": "caption"}]
        answer = sc._Result(claim=original.replace("Zero.That", "Zero. That"), verdict="SUPPORTED", confidence=.95)
        result = inventory.reconcile(saved, [answer], sc._Result)
        self.assertEqual((result[0].claim, result[0].verdict, result[0].confidence), (original, "SUPPORTED", .95))

    def test_verification_rejects_omitted_words_changed_numbers_and_duplicate_answers(self):
        saved = [{"claim": "Range is up to 3.0 km", "where": "caption"}]
        for claims in (["Range is up to 30 km"], ["3.0 km"], ["Range is 3.0 km"],
                       ["Range is up to 3.0 km", "Range is up to 3.0 km"]):
            with self.subTest(claims=claims):
                answers = [sc._Result(claim=claim, verdict="SUPPORTED", confidence=1) for claim in claims]
                self.assertEqual(inventory.reconcile(saved, answers, sc._Result)[0].verdict, "UNCLEAR")


class FormattedClaimScoringTests(unittest.IsolatedAsyncioTestCase):
    async def test_formatted_extraction_produces_evidence_findings_and_a_completed_score(self):
        original = "Reliable cooling during transport\nDesigned for demanding environments"
        claim = original.replace("\n", "; ")
        ask = AsyncMock(return_value=sc._Response(claims=[sc._Result(claim=claim, verdict="UNCLEAR")]))
        with (
            patch.object(inventory.db, "fetch_one", AsyncMock(return_value=None)),
            patch.object(inventory.db, "execute", AsyncMock(side_effect=lambda sql, key, claims: {"claims": claims})),
            patch.object(sc, "_ask", ask), patch.object(sc, "MODELS", ["test-model"]),
            patch.object(sc.newsroom_facts, "material", AsyncMock(return_value=None)),
            patch.object(cs, "applicable_rules", AsyncMock(return_value=[])),
            patch.object(cs, "_semantic_findings", AsyncMock(return_value=[])),
            patch.object(cs, "rulebook_version", AsyncMock(return_value="test")),
        ):
            result = await cs.check_compliance(ComplianceCheckRequest(text="Text in image 1: " + original, content_type="SOCIAL_POST"))
        self.assertFalse(result.ai_unavailable)
        self.assertEqual(result.score, 100 - cs.EVIDENCE_PENALTY)
        self.assertEqual(result.evidence_count, 1)
        self.assertEqual(result.violations[0].passage, original)
        self.assertFalse(result.is_compliant)  # Formatting repair does not turn missing evidence into support.

    async def test_unrecoverable_rewording_still_withholds_a_final_pass(self):
        ask = AsyncMock(return_value=sc._Response(claims=[sc._Result(claim="Range is 100 km", verdict="UNCLEAR")]))
        with (
            patch.object(inventory.db, "fetch_one", AsyncMock(return_value=None)),
            patch.object(inventory.db, "execute", AsyncMock()) as save,
            patch.object(sc, "_ask", ask), patch.object(sc, "MODELS", ["test-model"]),
            patch.object(sc.newsroom_facts, "material", AsyncMock(return_value=None)),
            patch.object(cs, "applicable_rules", AsyncMock(return_value=[])),
            patch.object(cs, "_semantic_findings", AsyncMock(return_value=[])),
            patch.object(cs, "rulebook_version", AsyncMock(return_value="test")),
        ):
            result = await cs.check_compliance(ComplianceCheckRequest(text="Range is 30 km", content_type="SOCIAL_POST"))
        self.assertTrue(result.ai_unavailable)
        self.assertFalse(result.is_compliant)
        save.assert_not_awaited()
