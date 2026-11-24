"""Rule 18 is optional for audiovisual content until the script explicitly cites a source."""
import unittest
from datetime import datetime, timezone
from unittest.mock import AsyncMock, patch
from uuid import uuid4

from models.compliance.compliance_model import ComplianceCheckRequest, ComplianceRule, ComplianceViolation
from services.compliance.evaluation import compliance_service as cs, rule18_policy as policy, source_check_service as sc


def rule(number):
    now = datetime.now(timezone.utc)
    return ComplianceRule(id=uuid4(), rule_number=number, title="Evidence", summary="Check evidence",
                          category="Evidence", severity="MEDIUM", traffic_light="YELLOW", created_at=now, updated_at=now)


class Rule18PolicyTests(unittest.TestCase):
    def test_scripts_and_videos_without_script_sources_skip_rule18(self):
        for kind in ("script", "VIDEO", "video_caption", "video_script", "script_doc"):
            with self.subTest(kind=kind):
                req = ComplianceCheckRequest(text="Drones can help monitor construction.", content_type=kind)
                self.assertFalse(policy.applies(req))
                self.assertFalse(cs._rule_applies(rule("Rule 18"), req))
                self.assertTrue(cs._rule_applies(rule("Rule 29"), req))

    def test_explicit_url_or_named_sources_in_script_enable_rule18(self):
        for citation in ("Sources used: https://example.com/report", "Source: Annual Technology Report 2026"):
            with self.subTest(citation=citation):
                req = ComplianceCheckRequest(text="Script body\n" + citation, content_type="script")
                self.assertTrue(policy.applies(req))
                req = ComplianceCheckRequest(text="Video transcript", content_type="VIDEO", source_script="Script body\n" + citation)
                self.assertTrue(policy.applies(req))

    def test_empty_template_source_field_does_not_enable_rule18(self):
        for value in ("Sources used (optional): [List the source names and links used to create this script.]",
                      "Sources used: None", "Learn more: https://example.com"):
            with self.subTest(value=value):
                self.assertFalse(policy.applies(ComplianceCheckRequest(text=value, content_type="script")))

    def test_caption_links_and_attached_sources_do_not_replace_explicit_script_citations(self):
        req = ComplianceCheckRequest(text="Sources: https://example.com/report", content_type="VIDEO",
                                     source_script="An uncited script")
        self.assertFalse(policy.applies(req))

    def test_ordinary_posts_and_images_keep_rule18(self):
        for kind in ("SOCIAL_POST", "IMAGE", "imagery_concept"):
            self.assertTrue(policy.applies(ComplianceCheckRequest(text="A claim", content_type=kind)))
        self.assertFalse(policy.applies(ComplianceCheckRequest(text="A claim", content_type="SOCIAL_POST", content_parts=["video"])))


class Rule18ScoringTests(unittest.IsolatedAsyncioTestCase):
    async def check(self, kind, source_script=None):
        finding = ComplianceViolation(rule_id=uuid4(), rule_number="Rule 18", title="Claim needs evidence",
            category="Source Check", severity="MEDIUM", traffic_light="YELLOW", status="EVIDENCE_REQUIRED",
            flagged_terms=["A claim"], explanation="Evidence missing", penalty=0)
        source = AsyncMock(return_value=sc.SourceCheckResult(unbacked=[finding]))
        with (
            patch.object(cs, "list_compliance_rules", AsyncMock(return_value=[rule("Rule 18")])),
            patch.object(cs, "_semantic_findings", AsyncMock(return_value=[])),
            patch.object(sc, "run", source),
            patch.object(cs, "rulebook_version", AsyncMock(return_value="test")),
        ):
            result = await cs.check_compliance(ComplianceCheckRequest(text="A script about construction.",
                content_type=kind, source_script=source_script))
        return result, source

    async def test_uncited_script_and_video_have_no_rule18_findings_deductions_or_source_calls(self):
        for kind in ("script", "VIDEO"):
            with self.subTest(kind=kind):
                result, source = await self.check(kind)
                source.assert_not_awaited()
                self.assertEqual(result.score, 100)
                self.assertFalse(result.ai_unavailable)
                self.assertEqual(result.violations, [])
                self.assertEqual(result.manual_review_rules, [])

    async def test_cited_script_retains_rule18_evidence_findings_and_scoring(self):
        result, source = await self.check("script", "Sources: https://example.com/report")
        source.assert_awaited_once()
        self.assertEqual(result.score, 100 - cs.EVIDENCE_PENALTY)
        self.assertEqual(result.evidence_count, 1)

    async def test_ordinary_post_still_checks_uncited_claims(self):
        result, source = await self.check("SOCIAL_POST")
        source.assert_awaited_once()
        self.assertEqual(result.evidence_count, 1)
