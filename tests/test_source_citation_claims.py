"""Citation metadata identifies a source; it is never itself a claim requiring source-body support."""
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock
from tests.test_claim_inventory import manifest

from services.compliance.evaluation import citation_finder as cf, source_post_checks as parts, source_check_service as sc

CREDIT = "Source: U.S. Army / September 2, 2026 / army.mil/article/295038"
SOURCE = "The assessment lasted two weeks. The team supplied power and data over a tether."


def result(claim, verdict="SUPPORTED"):
    return sc._Result(claim=claim, where="image", verdict=verdict, evidence=SOURCE, confidence=1.0)


class CitationClaimGuards(unittest.TestCase):
    def test_source_credit_is_filtered_before_missing_number_checks(self):
        self.assertEqual(sc._to_claims([result(CREDIT)], SOURCE), [])

    def test_quote_and_image_marker_do_not_turn_metadata_into_a_claim(self):
        for text in ['"' + CREDIT + '"', 'Text in image 1: ' + CREDIT, 'Sources:\nhttps://example.com/reports/987654']:
            with self.subTest(text=text):
                self.assertTrue(cf.is_reference_only(text))
                self.assertEqual(sc._to_claims([result(text)], SOURCE), [])

    def test_mismatched_substantive_figures_still_require_review(self):
        claims = sc._to_claims([result("The assessment lasted 295038 days.")], SOURCE)
        self.assertEqual(len(claims), 1)
        self.assertEqual(claims[0].verdict, "UNCLEAR")
        self.assertIn("295038", claims[0].note)

    def test_supported_real_duration_is_retained(self):
        claims = sc._to_claims([result("The assessment lasted two weeks.")], SOURCE)
        self.assertEqual(len(claims), 1)
        self.assertEqual(claims[0].verdict, "SUPPORTED")

    def test_a_real_claim_before_an_inline_credit_is_not_discarded(self):
        text = "The assessment lasted two weeks. Source: https://example.com/report/123"
        self.assertFalse(cf.is_reference_only(text))
        self.assertEqual(cf.without_citation_text(text), "The assessment lasted two weeks.")

    def test_image_marker_is_preserved_when_the_source_line_is_first(self):
        text = "Text in image 1: " + CREDIT + "\nA duration of 99 weeks is claimed."
        clean = cf.without_citation_text(text)
        self.assertIn("Text in image 1:", clean)
        self.assertIn("99 weeks", clean)
        self.assertNotIn("295038", clean)

    def test_source_heading_does_not_consume_the_next_image(self):
        text = "Sources:\nhttps://example.com/report/123\nText in image 2: Our output increased 50%."
        self.assertIn("Our output increased 50%.", cf.without_citation_text(text))


class ClaimComparisonInputs(unittest.IsolatedAsyncioTestCase):
    async def test_comparison_receives_claims_but_not_image_or_attached_citation_metadata(self):
        text = "Caption claim.\nText in image 1: The assessment lasted two weeks.\n" + CREDIT + "\n\nSources:\nhttps://army.mil/article/295038"
        compare = AsyncMock(return_value=([], "test-model"))
        await parts.compare_parts(text, "Army article", "Report", "https://army.mil/article/295038", "2026-09-02", SOURCE, compare, manifest=manifest(text, ["factual", "factual", "citation", "citation", "citation"]))
        bodies = [call.args[0] for call in compare.await_args_list]
        self.assertTrue(any("two weeks" in body for body in bodies))
        import json
        claims = [c for body in bodies for c in json.loads(body[body.index("{"):])["claims"]]
        self.assertTrue(all("295038" not in c["wording"] and "Source:" not in c["wording"] for c in claims))

    async def test_citation_only_image_needs_no_claim_comparison(self):
        compare = AsyncMock()
        claims, model, failed = await parts.compare_parts("Text in image 1: " + CREDIT, "Report", None, None, None, SOURCE, compare, manifest=manifest("Text in image 1: " + CREDIT, ["citation"]))
        self.assertEqual(claims, [])
        self.assertFalse(failed)
        compare.assert_not_awaited()

    async def test_actual_model_failure_is_still_reported(self):
        compare = AsyncMock(return_value=(None, None))
        claims, _, failed = await parts.compare_parts("The assessment lasted 99 weeks.", "Report", None, None, None, SOURCE, compare, manifest=manifest("The assessment lasted 99 weeks."))
        self.assertEqual(claims[0].verdict, "UNCLEAR")
        self.assertTrue(failed)
