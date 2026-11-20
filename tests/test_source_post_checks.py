"""Source reruns must cover image capability claims as well as supported caption claims."""
import unittest
from unittest.mock import AsyncMock, patch
from models.compliance.compliance_model import ProvidedSource, SourceCheck
from services.compliance.evaluation import source_check_service as sc, source_post_checks as coverage


class SourcePartChecks(unittest.IsolatedAsyncioTestCase):
    async def run_check(self, text, compare):
        with patch.object(sc, "_compare", compare), \
             patch.object(sc.newsroom_facts, "material", AsyncMock(return_value=None)), \
             patch.object(sc.source_passages, "EMBED_ENABLED", False):
            return await sc.run(text, [ProvidedSource(name="Company product page", text="Drones support rescue operations.")])

    def test_image_capability_list_needs_evidence_without_a_source(self):
        post = "test\nText in image 1: ZENODRONE\nSupply Delivery\nCommunications Support"
        findings = coverage.unsourced_image_findings(post, [])
        self.assertEqual(findings, [])
        source = SourceCheck(reference="Product page", kind="URL", where="source_links", status="READ")
        self.assertEqual(coverage.unsourced_image_findings(post, [source]), [])

    async def test_combined_result_keeps_caption_and_image_claims(self):
        answers = [sc._Result(claim="Drones support rescue operations", verdict="SUPPORTED",
                              evidence="Drones support rescue operations", confidence=0.95),
                   sc._Result(claim="Supply Delivery", where="image", verdict="NOT_FOUND", confidence=0.9)]
        compare = AsyncMock(return_value=(answers, "test-model"))
        post = "Drones support rescue operations.\nText in image 1: Supply Delivery"
        result = await self.run_check(post, compare)
        compare.assert_awaited_once()
        self.assertEqual(compare.call_args.args[0], post)
        self.assertEqual([(c.claim, c.where) for c in result.checks[0].claims], [
            ("Drones support rescue operations", "caption"), ("Supply Delivery", "image")])
        self.assertFalse(result.ai_failed)

    async def test_carousel_images_and_caption_are_compared_together(self):
        compare = AsyncMock(return_value=([], "test-model"))
        post = "Caption\nText in image 1: first\nText in image 2: second"
        await self.run_check(post, compare)
        compare.assert_awaited_once()
        self.assertEqual(compare.call_args.args[0], post)

    async def test_combined_comparison_failure_is_reported(self):
        compare = AsyncMock(return_value=(None, None))
        result = await self.run_check("Caption\nText in image 1: capability", compare)
        self.assertTrue(result.ai_failed)
        self.assertIn("No AI model", result.checks[0].message)
        self.assertNotIn("No claim", result.checks[0].message)

    async def test_caption_edits_update_the_combined_input(self):
        compare = AsyncMock(return_value=([], "test-model"))
        await self.run_check("First caption\nText in image 1: capability", compare)
        await self.run_check("Edited caption\nText in image 1: capability", compare)
        calls = compare.await_args_list
        self.assertNotEqual(calls[0].args[0], calls[1].args[0])
        self.assertIn("Text in image 1: capability", calls[1].args[0])

    async def test_removing_an_image_does_not_reuse_its_previous_findings(self):
        compare = AsyncMock(return_value=([], "test-model"))
        result = await self.run_check("Caption with the image removed", compare)
        compare.assert_awaited_once()
        self.assertEqual(result.checks[0].claims, [])

    async def test_caption_and_image_occurrences_of_a_claim_are_kept_separate(self):
        claim = sc._Result(claim="One capability", verdict="NOT_FOUND", confidence=0.9)
        compare = AsyncMock(return_value=([claim, claim.model_copy(update={"where": "image"})], "test-model"))
        claims, model, failed = await coverage.compare_parts(
            "One capability\nText in image 1: One capability", "source", None, None, None, "source text", compare)
        self.assertEqual([c.where for c in claims], ["caption", "image"])
        self.assertEqual(model, "test-model")
        self.assertFalse(failed)


class StableImageCoverage(unittest.IsolatedAsyncioTestCase):
    LABELS = ["Search & Tracking", "Supply Delivery", "Communications Support", "Rescue Support", "Emergency Response"]

    async def test_adjacent_labels_do_not_force_additional_claims(self):
        body = "Text in image 1:\n" + "\n".join(self.LABELS)
        compare = AsyncMock(return_value=([], "model"))
        claims, _, failed = await coverage.compare_parts(body, "source", None, None, None, "source", compare)
        self.assertEqual(claims, [])
        compare.assert_awaited_once()
        self.assertFalse(failed)

    def test_body_copy_and_logos_do_not_become_capability_list_items(self):
        from services.compliance.evaluation import source_image_claims as image
        body = ("Text in image 1: ZENADRONE\nMILITARY\nRESCUE DRONES\n"
                "Military rescue drones can help\nlocate, assess and support rescue\n"
                "operations involving injured or\nstranded personnel in high-risk\n"
                "combat zones or disaster areas while\nreducing human exposure to\ndangerous conditions\n")
        self.assertEqual(image.labels(body + "\n".join(self.LABELS)), self.LABELS)

    def test_generic_real_evidence_does_not_support_another_capability(self):
        claim = sc._Result(claim="Communications Support", where="image", verdict="SUPPORTED",
                           evidence="Drones support rescue operations", confidence=0.95)
        checked = sc._to_claims([claim], "Drones support rescue operations.")[0]
        self.assertEqual(checked.verdict, "UNCLEAR")
        self.assertIn("specific capability", checked.note)

    def test_invented_short_evidence_is_not_a_supported_match(self):
        claim = sc._Result(claim="Supply Delivery", where="image", verdict="SUPPORTED",
                           evidence="Supply delivery", confidence=0.95)
        self.assertEqual(sc._to_claims([claim], "Drones support rescue operations.")[0].verdict, "UNCLEAR")

    def test_evidence_for_the_specific_capability_remains_supported(self):
        claim = sc._Result(claim="Supply Delivery", where="image", verdict="SUPPORTED",
                           evidence="The aircraft can deliver supplies", confidence=0.95)
        checked = sc._to_claims([claim], "The aircraft can deliver supplies.")[0]
        self.assertEqual(checked.verdict, "SUPPORTED")

    def test_lowercase_capability_lists_are_kept(self):
        from services.compliance.evaluation import source_image_claims as image
        self.assertEqual(image.labels("supply delivery\ncommunications support"),
                         ["supply delivery", "communications support"])


    def test_short_capability_labels_are_used_to_find_source_passages(self):
        statements = sc.source_passages._statements("Text in image 1:\nSupply Delivery\nCommunications Support")
        self.assertIn("Supply Delivery", statements)
        self.assertIn("Communications Support", statements)
