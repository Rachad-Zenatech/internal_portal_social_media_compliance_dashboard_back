"""Source-panel comparisons must use the full compliance claim path."""
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from uuid import uuid4

from models.compliance.compliance_model import SourceCheck
from services.compliance.evaluation import source_check_service as sc, source_post_checks
from services.compliance.submissions import image_source_checks, source_link_service as links
from services.compliance.submissions import submission_service, submission_check_material


class SourceLinkCompliancePathTests(unittest.IsolatedAsyncioTestCase):
    async def test_scan_ocr_is_not_added_to_the_saved_posts_claim_inventory(self):
        row = {"id": uuid4()}
        with patch.object(submission_service, "_get_owned_row", AsyncMock(return_value=row)), \
             patch.object(submission_check_material, "load", AsyncMock(return_value=SimpleNamespace(
                 text="Saved caption\nText in image 1: Saved image"))) as load:
            material = await links.submission_material(row["id"], uuid4(), image_text="Unattached scan text")
        self.assertNotIn("Unattached scan text", material)
        load.assert_awaited_once_with(row)

    async def test_quick_and_full_checks_use_identical_parts_and_claim_locations(self):
        post = "Caption claim\nText in image 1: Image claim\nText in image 2: Other claim"
        compare = AsyncMock(side_effect=lambda body, *args: ([sc._Result(
            claim=body.split(": ")[-1], verdict="NOT_FOUND")], "model"))
        check = SourceCheck(reference="https://example.com", kind="URL", where="source_links", status="READ")
        with patch.object(sc, "_compare", compare), \
             patch.object(sc.source_passages, "EMBED_ENABLED", False):
            quick = await links.compare_with_source(check, post, "source", "Article text")
            quick_bodies = [call.args[0] for call in compare.await_args_list]
            compare.reset_mock()
            full, _, _ = await source_post_checks.compare_parts(
                post, "source", None, None, None, "Article text", compare)
        self.assertEqual(quick_bodies, [post])
        self.assertEqual(quick_bodies, [call.args[0] for call in compare.await_args_list])
        self.assertEqual([(c.claim, c.where) for c in quick.claims], [(c.claim, c.where) for c in full])

    async def test_combined_comparison_failure_is_visible(self):
        compare = AsyncMock(return_value=(None, None))
        check = SourceCheck(reference="source", kind="DOCUMENT", where="attached", status="READ")
        with patch.object(sc, "_compare", compare):
            checked = await links.compare_with_source(check, "Caption\nText in image 1: Claim", "source", "Article")
        self.assertIn("hasn't been compared", checked.message)

    async def test_source_discovery_succeeds_when_saved_post_has_no_claims(self):
        check = SourceCheck(reference="https://example.com", kind="URL", where="source_links", status="READ")
        with patch.object(links, "check_submission_source_link", AsyncMock(return_value=check)), \
             patch("services.compliance.submissions.source_trust_service.rank_by_trust", AsyncMock(
                 side_effect=lambda urls: urls)):
            chosen, checks = await image_source_checks.check_candidates(
                uuid4(), [check.reference], uuid4(), "Unattached scanned image")
        self.assertIs(chosen, check)
        self.assertEqual(checks, [check])

    async def test_scanning_does_not_replace_a_sources_verdict_with_another_sources_support(self):
        primary = SourceCheck(reference="https://example.com/one", kind="URL", where="source_links", status="READ",
            claims=[sc.SourceClaim(claim="Range is 30 km", verdict="NOT_FOUND")])
        other = primary.model_copy(update={"reference": "https://example.com/two", "claims": [
            sc.SourceClaim(claim="Range is 30 km", verdict="SUPPORTED")]})
        with patch.object(links, "check_submission_source_link", AsyncMock(side_effect=[primary, other])), \
             patch("services.compliance.submissions.source_trust_service.rank_by_trust", AsyncMock(
                 side_effect=lambda urls: urls)), patch.object(image_source_checks, "MAX_SOURCES", 2):
            chosen, checks = await image_source_checks.check_candidates(
                uuid4(), [primary.reference, other.reference], uuid4(), "Scanned text")
        self.assertIs(chosen, primary)
        self.assertEqual(chosen.claims[0].verdict, "NOT_FOUND")
        self.assertEqual(len(checks), 2)
