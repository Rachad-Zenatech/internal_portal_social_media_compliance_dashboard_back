"""Company branding cannot become a source requirement or an independent rule finding."""
import unittest
from unittest.mock import AsyncMock, patch
from uuid import uuid4

from models.compliance.compliance_model import ComplianceCheckRequest, ComplianceViolation
from services.compliance.evaluation import branding, compliance_service as compliance
from services.compliance.evaluation import source_check_service as sources, source_post_checks

BRANDING = ("ZenaTech", "ZENADRONE", "ZenaTech Inc.", "Nasdaq: Zena", "NASDAQ: $ZENA",
            "ZenaTech, Inc. (NASDAQ: ZENA)", "ZenaTech is listed on Nasdaq under ticker ZENA")


class BrandingTests(unittest.TestCase):
    def test_names_and_listing_labels_are_identification(self):
        for label in BRANDING:
            with self.subTest(label=label):
                self.assertTrue(branding.is_branding(label))
                self.assertEqual(branding.without_branding(label), "")

    def test_substantive_statements_remain(self):
        for statement in ("ZenaTech revenue increased 20%.", "ZenaDrone guarantees returns.",
                          "NASDAQ: ZENA shares will double.", "ZenaTech is Nasdaq approved."):
            with self.subTest(statement=statement):
                self.assertFalse(branding.is_branding(statement))
                self.assertEqual(branding.without_branding(statement), statement)

    def test_image_markers_and_actual_claims_are_preserved(self):
        text = "ZenaTech Inc.\nText in image 1: NASDAQ: ZENA\nRevenue increased 20%."
        self.assertEqual(branding.without_branding(text).strip(), "Text in image 1:\nRevenue increased 20%.")
        self.assertEqual(branding.without_branding("NASDAQ\nZENA"), "")

    def test_ai_cannot_turn_branding_into_an_unsupported_claim(self):
        for label in BRANDING:
            with self.subTest(label=label):
                claim = sources._Result(claim=label, verdict="NOT_FOUND", where="image", confidence=1)
                self.assertEqual(sources._to_claims([claim], "Article contents."), [])


class BrandingFlowTests(unittest.IsolatedAsyncioTestCase):
    async def test_branding_only_image_does_not_trigger_source_comparison(self):
        compare = AsyncMock()
        text = "ZenaTech Inc.\nText in image 1: NASDAQ: ZENA"
        claims, _, failed = await source_post_checks.compare_parts(text, "Source", None, None, None, "Article", compare)
        self.assertEqual(claims, [])
        self.assertFalse(failed)
        compare.assert_not_awaited()
        self.assertEqual(source_post_checks.unsourced_image_findings(text, []), [])

    async def test_compliance_strips_branding_and_drops_branding_only_ai_findings(self):
        finding = ComplianceViolation(
            rule_id=uuid4(), rule_number="Rule 18", title="Claim needs a source", category="Source Check",
            severity="MEDIUM", traffic_light="YELLOW", flagged_terms=["NASDAQ: ZENA"],
            explanation="No source", status="EVIDENCE_REQUIRED")
        semantic = AsyncMock(return_value=[finding])
        with (
            patch.object(compliance, "applicable_rules", AsyncMock(return_value=[])),
            patch.object(compliance, "rulebook_version", AsyncMock(return_value="test")),
            patch.object(compliance, "_semantic_findings", semantic),
            patch.object(sources, "run", AsyncMock(return_value=sources.SourceCheckResult())),
        ):
            result = await compliance.check_compliance(ComplianceCheckRequest(text="ZenaTech Inc.\nNASDAQ: ZENA"))
        self.assertEqual(semantic.await_args.args[0].text.strip(), "")
        self.assertEqual(result.violations, [])
        self.assertEqual(result.score, 100)
