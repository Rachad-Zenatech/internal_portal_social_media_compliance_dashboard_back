import unittest
from unittest.mock import AsyncMock, patch

from datetime import datetime, timezone
from uuid import uuid4

from models.compliance.compliance_model import ComplianceCheckRequest, ComplianceRule
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


KEYS = {c.rule_id: c.key for c in claim_checks.CHECKS}


def fired(text):
    """The checks that fire, by key: a check's rule number can be shared with another check."""
    return {KEYS[v.rule_id]: v for v in claim_checks.find_claims(text)}


class ClaimCheckTests(unittest.TestCase):
    def test_the_contract_graphic_trips_every_check(self):
        found = fired(CONTRACT_GRAPHIC)

        # The performance-figure check looks for measured results, which the graphic doesn't carry.
        self.assertEqual(set(found), {c.key for c in claim_checks.CHECKS if c.key != "performance-figure"})
        self.assertIn("$25m", found["financial-figure"].flagged_terms)
        self.assertIn("major global company", found["unnamed-counterparty"].flagged_terms)
        self.assertIn("officially approved", found["unqualified-approval"].flagged_terms)
        self.assertIn("already in deployment", found["deployment-status"].flagged_terms)

    def test_measured_results_and_figures_need_a_source_but_cost_nothing(self):
        for text, check in [
            ("Our drone reduces inventory counting time by 60%.", "performance-figure"),
            ("It is 40% faster than manual counting.", "performance-figure"),
            ("Replaced a $38,000 handgrip with a $30 controller.", "financial-figure"),
        ]:
            with self.subTest(text=text):
                finding = fired(text)[check]
                self.assertEqual((finding.status, finding.penalty), ("EVIDENCE_REQUIRED", 0))
        self.assertNotIn("performance-figure", fired("Save 50% off today"))

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
        self.assertNotIn("forward-looking-financial", fired(text))
        self.assertIn("forward-looking-financial", fired("We expect revenue to grow next year."))

    def test_the_words_forward_looking_alone_are_not_safe_harbor(self):
        self.assertIn("forward-looking-financial", fired("Revenue will double this year. We're a forward-looking team!"))
        # Canada's term for it counts too.
        self.assertNotIn("forward-looking-financial", fired(
            "Revenue will double this year. This post contains forward-looking information; results may differ."))

    def test_an_approval_that_names_its_issuer_is_fine(self):
        self.assertNotIn("unqualified-approval", fired("Approved by the FAA under a Part 107 waiver for beyond-line-of-sight flights."))
        self.assertIn("unqualified-approval", fired("Our drone is now fully certified."))

    def test_each_check_keeps_the_same_id_so_dismissals_stick(self):
        ids = [c.rule_id for c in claim_checks.CHECKS]
        self.assertEqual(len(set(ids)), len(ids))
        self.assertEqual(claim_checks.CHECKS[0].rule_id, claim_checks.FORWARD_LOOKING_ID)

    def test_each_check_is_labelled_with_its_rulebook_rule(self):
        self.assertEqual(
            {c.key: c.rule_number for c in claim_checks.CHECKS},
            {
                "forward-looking-financial": "Rule 25",
                "contract-announcement": "Rule 6",
                "unqualified-approval": "Rule 26",
                "financial-figure": "Rule 18",
                "performance-figure": "Rule 24",
                "unnamed-counterparty": "Rule 18",
                "deployment-status": "Rule 2",
            },
        )


class ScoreWithClaimChecksTests(unittest.IsolatedAsyncioTestCase):
    async def check(self, text):
        # No vector store or model: only the wording-based findings.
        with (
            patch.object(compliance_service, "applicable_rules", new=AsyncMock(return_value=[])),
            patch.object(compliance_service, "rulebook_version", new=AsyncMock(return_value=None)),
            patch.object(compliance_service, "_semantic_findings", new=AsyncMock(return_value=[])),
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

    async def test_figures_need_a_source_but_are_not_violations(self):
        result = await self.check("A typical battery swap costs $30 while older sensors cost $38,000.")

        self.assertEqual((result.violations_count, result.evidence_count), (0, 1))
        self.assertEqual(result.score, 100)
        # Something still needs a look, so it isn't green, and it can't be approved by accident as "no issues".
        self.assertEqual(result.traffic_light, "YELLOW")
        self.assertFalse(result.is_compliant)
        self.assertEqual(result.violations[0].status, "EVIDENCE_REQUIRED")

    async def test_semantic_matches_are_things_to_check_not_violations(self):
        now = datetime.now(timezone.utc)
        rule = ComplianceRule(
            id=uuid4(), rule_number="Rule 16", category="Claims", title="Separate Vision from Current Capability",
            summary="s", created_at=now, updated_at=now, severity="HIGH", traffic_light="YELLOW",
        )
        answer = type("Resp", (), {"text": (
            '{"semantic_violations": ['
            '{"rule_number": "Rule 16", "quote": "our drones already guard every border", "explanation": "Presents a '
            'future capability as current.", "confidence": 0.9},'
            '{"rule_number": "Rule 16", "quote": "words that are not in the post", "explanation": "x", "confidence": 0.99}'
            "]}"
        )})()
        await compliance_service._ai_check_cache.clear()
        with (
            patch.object(compliance_service, "applicable_rules", new=AsyncMock(return_value=[rule])),
            patch.object(compliance_service, "rulebook_version", new=AsyncMock(return_value=None)),
            patch.object(compliance_service, "_vector_context_for_check", new=AsyncMock(return_value=([rule], []))),
            patch.object(compliance_service, "generate_content_with_fallback", new=AsyncMock(return_value=answer)),
            patch.dict("os.environ", {"GEMINI_API_KEY": "test-key"}),
        ):
            result = await compliance_service.check_compliance(
                ComplianceCheckRequest(text="Good news: our drones already guard every border.")
            )

        # One finding: the quote that is in the post. It costs nothing and doesn't block.
        self.assertEqual([v.status for v in result.violations], ["NEEDS_REVIEW"])
        self.assertEqual((result.violations_count, result.review_count, result.score), (0, 1, 100))
        self.assertEqual(result.violations[0].flagged_terms, ["our drones already guard every border"])
        self.assertIn("by meaning", result.violations[0].explanation)


if __name__ == "__main__":
    unittest.main()
