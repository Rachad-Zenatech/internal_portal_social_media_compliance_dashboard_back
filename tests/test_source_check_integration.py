"""Source findings integrated with compliance scoring and visibility."""
import unittest
from unittest.mock import AsyncMock, patch

from models.compliance.compliance_model import ComplianceCheckRequest, SourceCheck, SourceClaim
from services.compliance.evaluation import compliance_service, source_check_service as sc


class InTheCheckTests(unittest.IsolatedAsyncioTestCase):
    """The source check inside check_compliance: its findings, its score and its place in the response."""

    async def check(self, text, source_checks, ai_failed=False):
        out = sc.SourceCheckResult(checks=source_checks, ai_failed=ai_failed)
        with (
            patch.object(compliance_service, "applicable_rules", new=AsyncMock(return_value=[])),
            patch.object(compliance_service, "rulebook_version", new=AsyncMock(return_value=None)),
            patch.object(compliance_service, "_semantic_findings", new=AsyncMock(return_value=[])),
            patch.object(compliance_service.source_check_service, "run", new=AsyncMock(return_value=out)),
        ):
            return await compliance_service.check_compliance(ComplianceCheckRequest(text=text))

    def read(self, *claims):
        return [SourceCheck(reference="https://www.zenatech.com/a-release", kind="URL", status="READ", title="The release",
                            claims=list(claims))]

    async def test_a_contradicted_claim_takes_20_points_and_the_response_carries_the_source_check(self):
        claim = SourceClaim(claim="pilots are underway", verdict="CONTRADICTED", evidence="pilots expected in 2027",
                            note="The source says 2027.", confidence=0.9)
        r = await self.check("Pilots are underway.", self.read(claim))
        self.assertEqual(r.score, 80)
        self.assertEqual(r.violations_count, 1)
        self.assertEqual(r.source_checks[0].claims[0].verdict, "CONTRADICTED")

    async def test_a_supported_claim_costs_nothing(self):
        claim = SourceClaim(claim="pilots expected in 2027", verdict="SUPPORTED", evidence="same", confidence=0.9)
        r = await self.check("Pilots expected in 2027.", self.read(claim))
        self.assertEqual((r.score, r.violations_count), (100, 0))

    async def test_a_source_that_couldnt_be_checked_is_for_a_person_and_costs_nothing(self):
        for source_status in ("NOT_PROVIDED", "UNREADABLE"):
            with self.subTest(status=source_status):
                named = [SourceCheck(reference="Project Agincourt memo", kind="NAMED", where="image",
                                     status=source_status, message="Source material unavailable.")]
                r = await self.check("Oct 2027.\nSource: Project Agincourt memo", named)
                self.assertEqual(r.score, 100)
                self.assertEqual(r.review_count, 1)
                self.assertFalse(r.is_compliant)
                self.assertEqual(r.traffic_light, "YELLOW")
                self.assertEqual(r.violations[0].penalty, 0)
                self.assertIsNone(r.violations[0].evaluation)
                self.assertFalse(r.ai_unavailable)

    async def test_low_confidence_ai_source_judgments_remain_hidden(self):
        claim = SourceClaim(claim="pilots are underway", verdict="UNCLEAR", confidence=0.4)
        r = await self.check("Pilots are underway.", self.read(claim))
        self.assertEqual(r.score, 100)
        self.assertEqual(r.review_count, 0)
        self.assertEqual(r.violations, [])

    async def test_a_supported_figure_no_longer_needs_a_source(self):
        claim = SourceClaim(claim="the drone cuts counting time by 60%", verdict="SUPPORTED",
                            evidence="the release says counting time fell by 60%", confidence=0.9)
        text = "Our drone cuts counting time by 60%."
        without = await self.check(text, [])
        with_source = await self.check(text, self.read(claim))
        self.assertEqual(without.score, 95)
        self.assertEqual(with_source.score, 100)
        self.assertEqual(with_source.cleared_count, 1)

    async def test_a_source_no_model_could_read_makes_the_check_incomplete(self):
        r = await self.check("Pilots expected in 2027.", self.read(), ai_failed=True)
        self.assertTrue(r.ai_unavailable)
