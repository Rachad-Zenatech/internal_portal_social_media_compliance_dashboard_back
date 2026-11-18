"""Creator context feedback preserves actionable detail while keeping legacy analyses readable."""
import unittest
from models.compliance.sentiment_model import ClaimCheck, NormCheck, AudienceReading
from services.compliance.sentiment.config import INSTRUCTIONS, PROMPT_VERSION
from tests.test_sentiment_context_fit import report, with_context
from services.compliance.sentiment import evidence


class ActionableFeedback(unittest.TestCase):
    def test_existing_saved_checks_remain_readable(self):
        norm = NormCheck(ref="C3", aspect="Opening", norm="Use a clear hook", observation="Professional opening", status="FITS")
        audience = AudienceReading(ref="C4", audience="Investors", reading="Understands the product", reaction="RESONATES")
        self.assertIsNone(norm.improvement)
        self.assertIsNone(audience.evidence)

    def test_specific_edits_and_post_evidence_survive_verification(self):
        value = report()
        value.platform_norms.checks = [NormCheck(ref="C3", aspect="Opening", norm="Lead with the useful outcome", observation="The first sentence centres on the speaker", status="ADJUST", evidence="Twenty five years in software", improvement="Open with: See how drones inspect a site.")]
        value.audiences.readings = [AudienceReading(ref="C4", audience="Investors", reading="Could read this as current commercial deployment", reaction="CAUTIOUS", evidence="Pilots are flying our drones every day.", improvement="Clarify that these are pilot-programme flights.")]
        checked = evidence._check_report(value, with_context())
        self.assertEqual(checked.platform_norms.checks[0].evidence, "Twenty five years in software")
        self.assertIn("Open with", checked.platform_norms.checks[0].improvement)
        self.assertIn("pilot-programme", checked.audiences.readings[0].improvement)

    def test_an_invented_wording_quote_is_not_shown_as_post_evidence(self):
        value = report()
        value.platform_norms.checks = [NormCheck(ref="C3", aspect="Tone", norm="Be concrete", observation="Check the claim", status="ADJUST", evidence="Words that are not in this post", improvement="Use a supported claim.")]
        self.assertIsNone(evidence._check_report(value, with_context()).platform_norms.checks[0].evidence)

    def test_claim_improvements_are_supported_by_the_schema(self):
        claim = ClaimCheck(claim="The product is available today", result="CONFLICTS", note="Pilot only", improvement="The product is currently in a pilot programme.")
        self.assertEqual(claim.model_dump()["improvement"], "The product is currently in a pilot programme.")

    def test_new_prompt_requires_specific_edits_without_inventing_issues(self):
        self.assertEqual(PROMPT_VERSION, "sentiment-v16")
        for instruction in ("Every ADJUST needs improvement", "Every CAUTIOUS or NEGATIVE", "do not invent corrected facts", "Do not manufacture an improvement"):
            self.assertIn(instruction, INSTRUCTIONS)

    def test_visual_descriptions_are_not_misrepresented_as_spoken_quotes(self):
        value = report()
        value.platform_norms.checks = [NormCheck(ref="C3", aspect="Imagery", norm="Use a calm product scene", observation="A destroyed street dominates the image", status="ADJUST", evidence="Image 1 shows a destroyed street", evidence_type="VISUAL", improvement="Use a neutral product image or a non-combat rescue scene.")]
        checked = evidence._check_report(value, with_context(images=1))
        self.assertEqual(checked.platform_norms.checks[0].evidence_type, "VISUAL")
        self.assertIn("destroyed street", checked.platform_norms.checks[0].evidence)
