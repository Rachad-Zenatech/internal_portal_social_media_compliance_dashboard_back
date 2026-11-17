"""Source reruns must cover image capability claims as well as supported caption claims."""
import unittest
from unittest.mock import AsyncMock, patch
from models.compliance.compliance_model import ProvidedSource
from services.compliance.evaluation import source_check_service as sc, source_post_checks as coverage


class SourcePartChecks(unittest.IsolatedAsyncioTestCase):
    async def run_check(self, text, compare):
        with patch.object(sc, "_compare", compare), \
             patch.object(sc.newsroom_facts, "material", AsyncMock(return_value=None)), \
             patch.object(sc.source_passages, "EMBED_ENABLED", False):
            return await sc.run(text, [ProvidedSource(name="Company product page", text="Drones support rescue operations.")])

    async def test_supported_caption_does_not_hide_unsupported_image_capabilities(self):
        async def compare(post, *args):
            if not post.startswith("Text in image"):
                return [sc._Result(claim="Drones support rescue operations", verdict="SUPPORTED",
                                   evidence="Drones support rescue operations", confidence=0.95)], "test-model"
            return [sc._Result(claim=claim, verdict="NOT_FOUND", confidence=0.9)
                    for claim in ("Supply Delivery", "Communications Support")], "test-model"
        compare = AsyncMock(side_effect=compare)
        result = await self.run_check("Drones support rescue operations.\nText in image 1: Supply Delivery\nCommunications Support", compare)
        self.assertEqual(compare.await_count, 2)
        claims = result.checks[0].claims
        self.assertEqual([(c.claim, c.where) for c in claims], [
            ("Drones support rescue operations", "caption"),
            ("Supply Delivery", "image"), ("Communications Support", "image")])
        self.assertEqual([v.flagged_terms for v in sc.findings(result.checks)],
                         [["Supply Delivery"], ["Communications Support"]])
        self.assertFalse(result.ai_failed)

    async def test_each_carousel_image_gets_its_own_comparison(self):
        compare = AsyncMock(return_value=([], "test-model"))
        await self.run_check("Caption\nText in image 1: first\nText in image 2: second", compare)
        self.assertEqual(compare.await_count, 3)
        bodies = [call.args[0] for call in compare.await_args_list]
        self.assertEqual(bodies, ["Caption", "Text in image 1: first", "Text in image 2: second"])

    async def test_an_image_failure_does_not_look_like_a_complete_source_check(self):
        compare = AsyncMock(side_effect=[([], "caption-model"), (None, None)])
        result = await self.run_check("Caption\nText in image 1: capability", compare)
        self.assertTrue(result.ai_failed)
        self.assertIn("could not be compared", result.checks[0].message)
        self.assertNotIn("No claim", result.checks[0].message)

    async def test_caption_edits_do_not_change_the_image_comparison_input(self):
        compare = AsyncMock(return_value=([], "test-model"))
        await self.run_check("First caption\nText in image 1: capability", compare)
        await self.run_check("Edited caption\nText in image 1: capability", compare)
        calls = compare.await_args_list
        self.assertEqual(calls[1].args[0], calls[3].args[0])
        self.assertNotEqual(calls[0].args[0], calls[2].args[0])

    async def test_removing_an_image_does_not_reuse_its_previous_findings(self):
        compare = AsyncMock(return_value=([], "test-model"))
        result = await self.run_check("Caption with the image removed", compare)
        compare.assert_awaited_once()
        self.assertEqual(result.checks[0].claims, [])

    async def test_caption_and_image_occurrences_of_a_claim_are_kept_separate(self):
        claim = sc._Result(claim="One capability", verdict="NOT_FOUND", confidence=0.9)
        compare = AsyncMock(return_value=([claim], "test-model"))
        claims, model, failed = await coverage.compare_parts(
            "One capability\nText in image 1: One capability", "source", None, None, None, "source text", compare)
        self.assertEqual([c.where for c in claims], ["caption", "image"])
        self.assertEqual(model, "test-model")
        self.assertFalse(failed)


class StableImageCoverage(unittest.IsolatedAsyncioTestCase):
    LABELS = ["Search & Tracking", "Supply Delivery", "Communications Support", "Rescue Support", "Emergency Response"]

    async def test_omitted_list_items_remain_visible_across_different_model_answers(self):
        body = "Text in image 1:\n" + "\n".join(self.LABELS)
        for chosen in (self.LABELS[:3], self.LABELS[3:], []):
            answers = [sc._Result(claim=claim, where="image", verdict="NOT_FOUND", confidence=0.9) for claim in chosen]
            compare = AsyncMock(side_effect=[(answers, "first-model"), ([], "fallback-model")])
            with self.subTest(chosen=chosen):
                claims, _, failed = await coverage.compare_parts(body, "source", None, None, None, "product page", compare)
                self.assertEqual({c.claim for c in claims}, set(self.LABELS))
                self.assertFalse(failed)
                for claim in claims:
                    if claim.claim not in chosen:
                        self.assertEqual((claim.verdict, claim.confidence), ("UNCLEAR", 0))

    async def test_each_missing_capability_is_requested_explicitly(self):
        body = "Text in image 1:\n" + "\n".join(self.LABELS)
        compare = AsyncMock(side_effect=[([], "first"), ([], "second")])
        await coverage.compare_parts(body, "source", None, None, None, "source", compare)
        targeted = compare.await_args_list[1].args[0]
        for claim in self.LABELS:
            self.assertIn("- " + claim, targeted)

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

    async def test_compound_claim_cannot_hide_an_unsupported_capability(self):
        claim = sc._Result(claim="Drones offer supply delivery and communications support", where="image",
                           verdict="SUPPORTED", evidence="Drones support rescue operations", confidence=0.95)
        compare = AsyncMock(return_value=([claim], "model"))
        claims, _, _ = await coverage.compare_parts(
            "Text in image 1:\nSupply Delivery\nCommunications Support", "source", None, None, None,
            "Drones support rescue operations", compare)
        self.assertEqual(claims[0].verdict, "UNCLEAR")


    def test_lowercase_capability_lists_are_kept(self):
        from services.compliance.evaluation import source_image_claims as image
        self.assertEqual(image.labels("supply delivery\ncommunications support"),
                         ["supply delivery", "communications support"])


    def test_short_capability_labels_are_used_to_find_source_passages(self):
        statements = sc.source_passages._statements("Text in image 1:\nSupply Delivery\nCommunications Support")
        self.assertIn("Supply Delivery", statements)
        self.assertIn("Communications Support", statements)
