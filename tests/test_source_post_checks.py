"""Source reruns must cover image capability claims as well as supported caption claims."""
import unittest
from unittest.mock import AsyncMock, patch
from models.compliance.compliance_model import ProvidedSource, SourceCheck
from services.compliance.evaluation import source_check_service as sc, source_post_checks as coverage


class SourceInventoryCoverage(unittest.IsolatedAsyncioTestCase):
    async def test_full_post_and_all_images_share_one_inventory(self):
        from tests.test_claim_inventory import manifest
        text = "Caption fact.\nText in image 1: First capability.\nText in image 2: Second capability."
        saved = manifest(text)
        async def compare(payload, *_args):
            return [sc._Result(claim_id=c.id, verdict="NOT_FOUND", confidence=.9) for c in saved.claims], "model"
        with patch.object(coverage.claim_manifest, "get", AsyncMock(return_value=saved)) as get:
            claims, _, failed = await coverage.compare_parts(text, "report", None, None, None, "source", compare)
        get.assert_awaited_once_with(text)
        self.assertFalse(failed)
        self.assertEqual([c.where for c in claims], ["caption", "image", "image"])

    async def test_no_source_response_can_omit_a_soft_capability_silently(self):
        from tests.test_claim_inventory import manifest
        text = "Text in image 1: Could help locate personnel."
        saved = manifest(text)
        claims, _, failed = await coverage.compare_parts(text, "report", None, None, None, "source",
            AsyncMock(return_value=([], "model")), manifest=saved)
        self.assertTrue(failed)
        self.assertEqual(claims[0].claim, saved.claims[0].wording)
        self.assertEqual(claims[0].verdict, "UNCLEAR")


class StableImageCoverage(unittest.IsolatedAsyncioTestCase):
    LABELS = ["Search & Tracking", "Supply Delivery", "Communications Support", "Rescue Support", "Emergency Response"]



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
