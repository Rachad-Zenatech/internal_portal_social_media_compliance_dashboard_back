"""Source-free videos retain their Rule 18 policy while other flags may use automatic newsroom evidence."""
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from uuid import uuid4
from models.compliance.compliance_model import ComplianceViolation, SourceCheck, SourceClaim
from services.compliance.evaluation import source_evidence_lookup as lookup, source_check_service as sources
from services.compliance.evaluation import rule18_policy, evidence_reconciliation

CLAIM = "ZenaTech has secured three new transportation survey contracts."


def request(**changes):
    return SimpleNamespace(**{**dict(text=CLAIM, content_type="VIDEO", content_parts=["video"], sources=[],
        source_script=None, brand_code="ZenaTech"), **changes})


def fixture(number="Rule 6"):
    item = ComplianceViolation(rule_id=uuid4(), rule_number=number, title="Contract or deal announcement",
        category="Financial Disclosure", severity="HIGH", traffic_light="RED", status="VIOLATION", penalty=20,
        flagged_terms=["secured three new transportation survey contracts"], explanation="Must be publicly announced")
    rule = SimpleNamespace(id=item.rule_id, rule_number=number, title=item.title,
        summary="Only announce contracts already published by the company.", evaluation_guidance="Check the public source.")
    return item, rule


class SourceEvidenceLookupTests(unittest.IsolatedAsyncioTestCase):
    async def test_video_without_attached_link_still_checks_newsroom_for_rule6(self):
        req = request(); item, rule = fixture()
        self.assertFalse(rule18_policy.applies(req))
        source = SourceCheck(reference="Company newsroom", title="Published announcement", kind="URL", where="newsroom",
            authority="COMPANY", status="READ", claims=[SourceClaim(claim=CLAIM, verdict="SUPPORTED", evidence=CLAIM, confidence=.95)])
        result = sources.SourceCheckResult(checks=[source])
        with patch.object(sources, "run", AsyncMock(return_value=result)) as verify:
            verified, generated = await lookup.check(req, [item], [rule])
        self.assertIs(verified, result)
        self.assertEqual(generated, [])
        self.assertEqual([claim["claim"] for claim in verify.call_args.kwargs["claims"]], [CLAIM])
        item.passage = CLAIM
        evidence_reconciliation.clear_supported([item], verified.checks)
        self.assertEqual((item.status, item.penalty), ("CLEARED", 0))

    async def test_lookup_does_not_enable_general_rule18_findings(self):
        item, rule = fixture()
        unbacked = ComplianceViolation(rule_id=uuid4(), rule_number="Rule 18", title="Unrelated claim needs evidence",
            category="Source Check", explanation="No support", severity="MEDIUM", traffic_light="YELLOW", flagged_terms=[],
            status="EVIDENCE_REQUIRED", penalty=5)
        result = sources.SourceCheckResult(unbacked=[unbacked])
        with patch.object(sources, "run", AsyncMock(return_value=result)):
            _, findings = await lookup.check(request(), [item], [rule])
        self.assertEqual(findings, [])

    async def test_video_without_a_source_resolvable_flag_keeps_the_existing_exemption(self):
        with patch.object(sources, "run", AsyncMock()) as verify:
            result, findings = await lookup.check(request(), [], [])
        verify.assert_not_awaited()
        self.assertEqual((result.checks, findings), ([], []))

    async def test_obvious_content_flags_do_not_trigger_newsroom_search(self):
        item, rule = fixture("Rule 28")
        with patch.object(sources, "run", AsyncMock()) as verify:
            await lookup.check(request(), [item], [rule])
        verify.assert_not_awaited()

    async def test_normal_post_uses_original_complete_source_pipeline(self):
        result = sources.SourceCheckResult()
        req = request(content_type="TEXT_POST", content_parts=["caption"])
        with patch.object(rule18_policy, "check_sources", AsyncMock(return_value=result)) as verify:
            returned, findings = await lookup.check(req, [], [])
        self.assertIs(returned, result)
        verify.assert_awaited_once_with(req)
        self.assertEqual(findings, [])

    async def test_source_seeds_preserve_subject_numbers_and_negation(self):
        text = "ZenaTech has not secured three new transportation survey contracts; revenue of $503,000 is not confirmed."
        item, rule = fixture(); item.passage = text
        seeds = lookup.seeds(request(text=text), [item], [rule])
        self.assertEqual(len(seeds), 1)
        self.assertEqual(seeds[0]["claim"], text)
        self.assertTrue(seeds[0]["claim_id"])

    async def test_existing_video_financial_flag_gets_its_own_newsroom_check(self):
        from services.compliance.evaluation import claim_checks, financial_evidence
        caption = "ZenaTech has secured three contracts worth approximately $503,000."
        headline = "8 PROJECTS ACROSS 3 STATES REPRESENTING OVER US$500,000"
        ocr = "NASDAQ : $ZENA investor relations. as a service government " + headline + " our drone strategy"
        text = caption + "\nText in image 1:\n" + ocr
        items = claim_checks.find_claims(text)
        req = request(text=text)
        verified = SourceCheck(reference="Company announcement", kind="URL", where="newsroom", status="READ",
            authority="COMPANY", claims=[SourceClaim(claim=headline, where="image", verdict="SUPPORTED",
            evidence="Eight projects across three states total approximately $503,000.", confidence=.95)])
        result = sources.SourceCheckResult(checks=[verified])
        with patch.object(sources, "run", AsyncMock(return_value=result)) as verify:
            checked, generated = await lookup.check(req, items, [])
        inventory = verify.call_args.kwargs["claims"]
        self.assertIn(headline, [claim["claim"] for claim in inventory])
        image = next(claim for claim in inventory if claim["where"] == "image")
        self.assertEqual(image["context"], ocr)
        self.assertEqual(generated, [])
        financial_evidence.expand(items, checked.checks, text)
        evidence_reconciliation.clear_supported(items, checked.checks)
        cleared = [v for v in items if v.status == "CLEARED"]
        self.assertEqual([v.passage for v in cleared], [headline])
        self.assertTrue(any(v.status == "EVIDENCE_REQUIRED" and "$503,000" in v.passage for v in items))

    async def test_unrelated_rule18_flag_does_not_enable_general_video_extraction(self):
        item, rule = fixture("Rule 18")
        with patch.object(sources, "run", AsyncMock()) as verify:
            await lookup.check(request(), [item], [rule])
        verify.assert_not_awaited()
