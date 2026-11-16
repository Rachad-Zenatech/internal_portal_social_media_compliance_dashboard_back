import unittest
from unittest.mock import AsyncMock, patch

from models.compliance.compliance_model import SourceCheck, SourceClaim
from services.compliance.evaluation import newsroom_facts as nf
from services.compliance.evaluation import source_check_service as sc

MESA = {"title": "ZenaTech begins development of mobile agricultural drone operations trailer",
        "body": "ZenaTech has begun building a mobile agricultural drone operations trailer at its facility in Mesa, Arizona.",
        "url": "https://www.zenatech.com/mesa-trailer/"}
OTHER = {"title": "ZenaTech targets 2027 adjusted EBITDA breakeven", "body": "Revenue grew 425% in the first half.", "url": "https://x/"}
POST = "With our new Mesa office now open, we're building a manufacturing facility in Arizona for agricultural platforms."


def claim(text, verdict, evidence=""):
    return SourceClaim(claim=text, verdict=verdict, where="caption", evidence=evidence, confidence=0.9)


def check(reference, claims, **kw):
    return SourceCheck(reference=reference, kind="URL", status="READ", claims=claims, **kw)


class NewsroomFactTests(unittest.IsolatedAsyncioTestCase):
    def test_only_facts_close_to_the_post_are_picked(self):
        self.assertEqual(nf.rank(POST, [OTHER, MESA]), [MESA])
        self.assertEqual(nf.rank("Meet the team at the expo.", [MESA, OTHER]), [])

    def test_the_image_label_is_not_a_word_of_the_post(self):
        self.assertNotIn("image", nf._words("Text in image 1: hello"))

    def test_a_claim_the_post_never_makes_is_dropped(self):
        post = ("With TillerStack's Osra+, technicians can connect with remote experts and access guidance through "
                "smart glasses while keeping their hands free.")
        made = claim("TillerStack's Osra+ lets technicians connect with remote experts and access guidance through smart glasses", "SUPPORTED")
        invented = [claim("Stacklens is currently in the prototype and testing stage", "SUPPORTED"),
                    claim("Customer pilots for Stacklens are expected in 2027", "SUPPORTED"),
                    claim("Stacklens smart glasses are available for use.", "CONTRADICTED")]
        self.assertEqual(nf.in_post([made, *invented], post), [made])

    async def test_the_newsroom_is_a_source_with_the_close_facts_in_its_text(self):
        with patch.object(nf, "fetch_all", new=AsyncMock(return_value=[MESA, OTHER])):
            made = await nf.material(POST)
        check_, (label, text) = made
        self.assertTrue(nf.is_newsroom(check_))
        self.assertIn("Mesa, Arizona", text)
        self.assertNotIn("EBITDA", text)

    async def test_no_close_fact_means_no_source_and_a_failure_to_read_does_not_stop_the_check(self):
        with patch.object(nf, "fetch_all", new=AsyncMock(return_value=[OTHER])):
            self.assertIsNone(await nf.material(POST))
        with patch.object(nf, "fetch_all", new=AsyncMock(side_effect=RuntimeError("db down"))):
            self.assertIsNone(await nf.material(POST))


class NewsroomFindingsTests(unittest.TestCase):
    def test_a_claim_the_source_left_open_is_dropped_when_the_newsroom_supports_it(self):
        said = "Mesa office now open"
        source = check("https://x.com/a", [claim(said, "NOT_FOUND")])
        newsroom = check(nf.REFERENCE, [claim("The Mesa office is now open", "SUPPORTED", "facility in Mesa, Arizona")])
        self.assertEqual(sc.findings([source]).__len__(), 1)
        self.assertEqual(sc.findings([source, newsroom]), [])

    def test_a_claim_the_newsroom_does_not_cover_stays_as_the_source_left_it(self):
        source = check("https://x.com/a", [claim("Opens a plant in Ohio", "NOT_FOUND")])
        newsroom = check(nf.REFERENCE, [claim("The Mesa office is now open", "SUPPORTED")])
        self.assertEqual(len(sc.findings([source, newsroom])), 1)

    def test_the_newsroom_never_adds_a_not_found_finding_of_its_own(self):
        newsroom = check(nf.REFERENCE, [claim("Opens a plant in Ohio", "NOT_FOUND"), claim("Might grow", "UNCLEAR")])
        self.assertEqual(sc.findings([newsroom]), [])

    def test_a_claim_that_differs_from_the_newsroom_is_for_a_person_and_costs_nothing(self):
        newsroom = check(nf.REFERENCE, [claim("Revenue grew 60%", "CONTRADICTED", "Revenue grew 425%")])
        found = sc.findings([newsroom])
        self.assertEqual([(f.status, f.penalty) for f in found], [("NEEDS_REVIEW", 0)])
        self.assertIn("newsroom", found[0].explanation)

    def test_a_claim_that_contradicts_a_cited_source_still_costs_points(self):
        source = check("https://x.com/a", [claim("Revenue grew 60%", "CONTRADICTED", "Revenue grew 425%")])
        self.assertEqual([(f.status, f.penalty) for f in sc.findings([source])], [("VIOLATION", sc.PENALTY_CONTRADICTED)])


class PublishedFiguresTests(unittest.TestCase):
    def hit(self, number, terms):
        import uuid
        from models.compliance.compliance_model import ComplianceViolation

        return ComplianceViolation(
            rule_id=uuid.uuid4(), rule_number=number, title="t", category="c", severity="CRITICAL", traffic_light="RED",
            flagged_terms=terms, explanation="x", penalty=20, status="VIOLATION",
            passage="ZenaTech's DaaS locations have signed three new construction survey contracts.",
        )

    def supported(self, authority):
        said = "ZenaTech's DaaS locations have signed three new construction survey contracts on transportation projects."
        return check("https://www.zenatech.com/a/", [claim(said, "SUPPORTED", "signed three new construction survey contracts")],
                     authority=authority)

    def test_a_company_page_that_says_it_clears_a_non_public_data_hit(self):
        v = self.hit("Rule 6", ["signed three new construction survey contracts"])
        self.assertEqual(sc.clear_supported([v], [self.supported("COMPANY")]), 1)
        self.assertEqual((v.status, v.penalty, v.traffic_light), ("CLEARED", 0, "GREEN"))
        self.assertIn("already published", v.explanation)

    def test_a_source_that_is_not_the_company_or_official_does_not(self):
        v = self.hit("Rule 6", ["signed three new construction survey contracts"])
        self.assertEqual(sc.clear_supported([v], [self.supported("OTHER")]), 0)
        self.assertEqual(v.status, "VIOLATION")

    def test_other_rules_are_not_cleared_by_a_source(self):
        v = self.hit("Rule 99", ["signed three new construction survey contracts"])
        self.assertEqual(sc.clear_supported([v], [self.supported("COMPANY")]), 0)
        self.assertEqual(v.status, "VIOLATION")


class NotFoundCostsPointsTests(unittest.TestCase):
    def found(self, n):
        claims = [claim(f"Distinct claim number {i} about product {i}", "NOT_FOUND") for i in range(n)]
        return sc.findings([check("https://x.com/a", claims)])

    def test_each_claim_the_source_does_not_mention_costs_five_points(self):
        from services.compliance.evaluation import compliance_service as cs

        found = self.found(1)
        costing = cs._charge_unsupported_claims(found)
        self.assertEqual([v.penalty for v in costing], [cs.EVIDENCE_PENALTY])
        self.assertEqual(cs.EVIDENCE_PENALTY, 5)

    def test_they_share_the_cap_with_claims_that_need_a_source(self):
        from services.compliance.evaluation import compliance_service as cs

        found = self.found(4)
        costing = cs._charge_unsupported_claims(found)
        self.assertEqual(sum(v.penalty for v in costing), cs.EVIDENCE_CAP)
        self.assertEqual(sorted(v.penalty for v in found), [0, 0, 5, 5])

    def test_an_unclear_claim_and_a_claim_the_newsroom_supports_cost_nothing(self):
        from services.compliance.evaluation import compliance_service as cs

        unclear = sc.findings([check("https://x.com/a", [claim("Maybe a claim about gears", "UNCLEAR")])])
        self.assertEqual(cs._charge_unsupported_claims(unclear), [])


if __name__ == "__main__":
    unittest.main()
