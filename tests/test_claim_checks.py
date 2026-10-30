import unittest
from unittest.mock import AsyncMock, patch

from models.compliance.compliance_model import ComplianceCheckRequest
from services.compliance import claim_checks, compliance_service

# The text read from the "$25M contract" graphic, line breaks and all.
CONTRACT_GRAPHIC = """ZENA T T E C H
AI SOLUTIONS FOR A SMARTER TOMORROW
JUST SIGNED A
$25M CONTRACT
WITH A MAJOR GLOBAL COMPANY
$25M
CONTRACT VALUE
EXPECTED TO ADD
$10M+
IN REVENUE THIS YEAR
OFFICIALLY APPROVED
AND ALREADY IN DEPLOYMENT
A MAJOR DRIVER
OF GROWTH FOR ZENATECH"""


def fired(text):
    return {v.rule_number: v for v in claim_checks.find_claims(text)}


class ClaimCheckTests(unittest.TestCase):
    def test_the_contract_graphic_trips_every_check(self):
        found = fired(CONTRACT_GRAPHIC)

        self.assertEqual(set(found), {c.rule_number for c in claim_checks.CHECKS})
        self.assertIn("$25m", found["Claim check 4"].flagged_terms)
        self.assertIn("major global company", found["Claim check 5"].flagged_terms)
        self.assertIn("officially approved", found["Claim check 3"].flagged_terms)
        self.assertIn("already in deployment", found["Claim check 6"].flagged_terms)

    def test_ordinary_posts_are_left_alone(self):
        for text in [
            "Happy Thanksgiving from the ZenaTech family! What are you thankful for this year?",
            "Visit ZenaTech at booth C14 at the Dubai Airshow, November 17-21.",
            "Our team tested the ZenaDrone 1000 in windy conditions today.",
        ]:
            with self.subTest(text=text):
                self.assertEqual(fired(text), {})

    def test_safe_harbor_wording_clears_the_forward_looking_check(self):
        text = "We expect revenue to grow next year. This post contains forward-looking statements; actual results may differ materially."
        self.assertNotIn("Claim check 1", fired(text))
        self.assertIn("Claim check 1", fired("We expect revenue to grow next year."))

    def test_the_words_forward_looking_alone_are_not_safe_harbor(self):
        self.assertIn("Claim check 1", fired("Revenue will double this year. We're a forward-looking team!"))
        # Canada's term for it counts too.
        self.assertNotIn("Claim check 1", fired(
            "Revenue will double this year. This post contains forward-looking information; results may differ."))

    def test_an_approval_that_names_its_issuer_is_fine(self):
        self.assertNotIn("Claim check 3", fired("Approved by the FAA under a Part 107 waiver for beyond-line-of-sight flights."))
        self.assertIn("Claim check 3", fired("Our drone is now fully certified."))

    def test_each_check_keeps_the_same_id_so_dismissals_stick(self):
        ids = [c.rule_id for c in claim_checks.CHECKS]
        self.assertEqual(len(set(ids)), len(ids))
        self.assertEqual(claim_checks.CHECKS[0].rule_id, claim_checks.FORWARD_LOOKING_ID)


class ScoreWithClaimChecksTests(unittest.IsolatedAsyncioTestCase):
    async def check(self, text):
        with (
            patch.object(compliance_service, "applicable_rules", new=AsyncMock(return_value=[])),
            patch.object(compliance_service, "rulebook_version", new=AsyncMock(return_value=None)),
        ):
            return await compliance_service.check_compliance(ComplianceCheckRequest(text=text))

    async def test_the_contract_graphic_no_longer_scores_100(self):
        result = await self.check(CONTRACT_GRAPHIC)

        self.assertFalse(result.is_compliant)
        self.assertEqual(result.traffic_light, "YELLOW")
        self.assertLessEqual(result.score, 20)

    async def test_a_clean_caption_still_scores_100(self):
        result = await self.check("Happy Thanksgiving from the ZenaTech family!")
        self.assertEqual(result.score, 100)


if __name__ == "__main__":
    unittest.main()
