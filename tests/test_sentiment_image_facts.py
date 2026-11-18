"""Image and platform-caption claims must survive grounded sentiment fact verification."""
import unittest
from unittest.mock import AsyncMock, patch
from uuid import uuid4
from models.compliance.sentiment_model import ClaimCheck
from services.compliance.sentiment import evidence, input_builder
from tests.test_sentiment_context_fit import report, with_context
from tests.test_sentiment_strictness import inp as post

CLAIM = "Military rescue drones can support rescue operations."


class ImageFactClaims(unittest.TestCase):
    def test_claim_in_completed_ocr_is_not_dropped_as_no_claims(self):
        value = report()
        value.facts.claims = [ClaimCheck(claim=CLAIM, result="NO_FACT", note="Needs a source")]
        inp = with_context()
        inp["images"] = [{"file_id": "image", "name": "rescue.png", "ocr_text": CLAIM}]
        checked = evidence._check_report(value, inp)
        self.assertEqual(checked.facts.claims[0].claim, CLAIM)
        self.assertEqual(checked.facts.verdict, "UNVERIFIED")

    def test_unseen_image_claim_is_still_rejected(self):
        value = report()
        value.facts.claims = [ClaimCheck(claim=CLAIM, result="NO_FACT")]
        self.assertEqual(evidence._check_report(value, with_context()).facts.claims, [])

    def test_platform_specific_caption_is_valid_post_evidence(self):
        value = report()
        value.facts.claims = [ClaimCheck(claim=CLAIM, result="NO_FACT")]
        inp = with_context()
        inp["platform_captions"] = {"LINKEDIN": CLAIM}
        self.assertEqual(evidence._check_report(value, inp).facts.claims[0].claim, CLAIM)


class ImageInput(unittest.IsolatedAsyncioTestCase):
    async def test_only_completed_ocr_is_used(self):
        rows = [{"id": uuid4(), "original_name": "done.png", "image_text": CLAIM, "image_text_status": "COMPLETED"},
                {"id": uuid4(), "original_name": "pending.png", "image_text": "unfinished read", "image_text_status": "PROCESSING"}]
        with patch.object(input_builder, "fetch_all", AsyncMock(return_value=rows)):
            images = await input_builder._load_images(uuid4())
        self.assertEqual(images[0]["ocr_text"], CLAIM)
        self.assertEqual(images[1]["ocr_text"], "")

    async def test_prompt_includes_the_read_image_claims(self):
        inp = post()
        inp["images"] = [{"file_id": "image", "name": "rescue.png", "ocr_text": CLAIM}]
        prompt = input_builder._prompt(inp, images_attached=1)
        self.assertIn('"image_text"', prompt)
        self.assertIn(CLAIM, prompt)
