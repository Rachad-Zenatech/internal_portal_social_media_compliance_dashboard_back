"""Saved source inputs and cumulative evidence must describe the current check."""
import unittest
from uuid import uuid4
from unittest.mock import AsyncMock, patch

from models.compliance.compliance_model import ComplianceViolation, SourceCheck, SourceClaim
from services.compliance.submissions import submission_source_state as state, source_link_service as links
from services.compliance.evaluation import source_check_service as sc

A, B = "https://example.com/a", "https://example.com/b"


def check(url, verdict, claim="The battery lasts 10 hours", where="image"):
    return SourceCheck(reference=url, url=url, kind="URL", where="source_links", status="READ",
        claims=[SourceClaim(claim=claim, where=where, verdict=verdict, confidence=0.95, evidence="The battery lasts 10 hours")])


class SourceFreshnessTests(unittest.TestCase):
    def test_source_addition_removal_and_legacy_checks_are_stale(self):
        self.assertTrue(state.sources_changed({"source_links":[A]}, {"source_checks":[]}))
        self.assertTrue(state.sources_changed({"source_links":[]}, {"checked_source_links":[A]}))
        self.assertTrue(state.sources_changed({"source_links":[A,B]}, {"checked_source_links":[A]}))

    def test_same_sources_ignore_reordering_duplicates_and_fragment(self):
        self.assertFalse(state.sources_changed({"source_links":[B,A,A+"#section"]}, {"checked_source_links":[A,B]}))
        self.assertFalse(state.sources_changed({"source_links":[A]}, {"source_checks":[{"where":"source_links","reference":A}]}))
        self.assertFalse(state.sources_changed({"source_links":[A]}, None))

    def test_support_from_one_source_prevents_another_sources_silence_from_costing_points(self):
        for verdict in ("NOT_FOUND", "UNCLEAR"):
            with self.subTest(verdict=verdict):
                self.assertEqual(sc.findings([check(A,"SUPPORTED"),check(B,verdict)]), [])

    def test_missing_support_in_two_articles_is_one_global_finding(self):
        found=sc.findings([check(A,"NOT_FOUND"),check(B,"NOT_FOUND")])
        self.assertEqual(len(found),1)

    def test_relevant_but_ambiguous_evidence_is_not_a_second_missing_source_error(self):
        found=sc.findings([check(A,"NOT_FOUND"),check(B,"UNCLEAR")])
        self.assertEqual(len(found),1)
        self.assertEqual(found[0].title,"Source doesn't clearly settle the claim")

    def test_a_real_conflict_is_not_hidden_by_another_supporting_source(self):
        found=sc.findings([check(A,"SUPPORTED"),check(B,"CONTRADICTED")])
        self.assertEqual(len(found),1)
        self.assertEqual(found[0].status,"VIOLATION")

    def test_support_for_a_range_name_cannot_clear_a_broader_opening_claim(self):
        flag=ComplianceViolation(rule_id=uuid4(),rule_number="Rule 18",title="Claim needs evidence",category="Source Check",
            severity="MEDIUM",traffic_light="YELLOW",status="EVIDENCE_REQUIRED",penalty=5,
            flagged_terms=["The Army opened ranges in Utah, Michigan and Morocco"],explanation="Needs evidence")
        self.assertEqual(sc.clear_supported([flag],[check(A,"SUPPORTED","Michigan")]),0)


class AddedSourceAuditTests(unittest.IsolatedAsyncioTestCase):
    async def test_automatically_added_source_updates_content_revision_conditionally(self):
        with patch.object(links,"execute",AsyncMock()) as execute:
            await links.save_source_link_check(uuid4(),A,check(A,"SUPPORTED"),add_link=True)
        query=execute.call_args.args[0]
        self.assertIn("content_edited_at = CASE",query)
        self.assertIn("THEN content_edited_at ELSE NOW() END",query)
