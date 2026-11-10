import unittest
from datetime import datetime, timezone
from unittest.mock import AsyncMock, patch
from uuid import uuid4

from fastapi import HTTPException

from models.compliance.policy_model import PolicyRuleProposal
from services.compliance.policy import policy_service as svc

PAGE = """
<html><head><script>var x = 1;</script><style>p{}</style></head>
<body><nav>Home Help</nav><header>Menu</header>
<main><h1>Paid promotions</h1><p>Creators must disclose paid promotions using the paid promotion label.</p>
<ul><li>Sponsored content must be clearly marked.</li></ul></main>
<footer>Privacy Terms</footer></body></html>
"""

RULES = [
    {"id": uuid4(), "rule_number": "Rule 22", "title": "Comments, Replies and Amplification", "summary": "Replies follow the rules",
     "prohibited_terms": [], "required_disclaimers": []},
    {"id": uuid4(), "rule_number": "SMCP-1", "title": "Disclose paid promotions and sponsorships",
     "summary": "Paid promotions must carry the paid promotion label", "prohibited_terms": ["sponsored by"],
     "required_disclaimers": []},
]
TEXT = "Creators must disclose paid promotions using the paid promotion label. Sponsored content must be clearly marked."


class ReadingTests(unittest.TestCase):
    def test_keeps_the_content_and_drops_scripts_menus_and_footers(self):
        text = svc.html_to_text(PAGE)
        self.assertIn("Creators must disclose paid promotions", text)
        self.assertIn("Sponsored content must be clearly marked.", text)
        for gone in ("var x", "Home Help", "Menu", "Privacy Terms"):
            self.assertNotIn(gone, text)

    def test_the_fingerprint_ignores_spacing_and_case(self):
        self.assertEqual(svc.text_hash("Paid  Promotions\n label"), svc.text_hash("paid promotions label"))


class ProposalTests(unittest.TestCase):
    def proposal(self, **over):
        p = {"title": "Disclose paid promotions", "summary": "Mark paid promotions.", "severity": "HIGH",
             "traffic_light": "YELLOW", "evaluation_mode": "CONTEXT", "prohibited_terms": ["sponsored by"],
             "platforms": ["YOUTUBE"], "regions": [], "paid_ads_only": False,
             "source_quote": "Creators must disclose paid promotions using the paid promotion label.", "similar": []}
        p.update(over)
        return p

    def clean(self, p, platforms=("YOUTUBE", "YOUTUBE_SHORTS"), regions=("Europe",), default=()):
        return svc._clean_proposal(p, list(platforms), list(regions), TEXT, RULES, list(default))

    def test_a_quote_that_is_not_on_the_page_is_dropped(self):
        self.assertIsNone(self.clean(self.proposal(source_quote="Nothing like this is on the page at all."))["source_quote"])
        self.assertIsNotNone(self.clean(self.proposal())["source_quote"])

    def test_platforms_and_regions_stay_within_the_page(self):
        row = self.clean(self.proposal(platforms=["YOUTUBE", "TIKTOK"], regions=["Europe", "Canada"]))
        self.assertEqual((row["platforms"], row["regions"]), (["YOUTUBE"], ["Europe"]))

    def test_no_platform_named_means_every_platform_of_the_page(self):
        self.assertEqual(self.clean(self.proposal(platforms=[]))["platforms"], ["YOUTUBE", "YOUTUBE_SHORTS"])

    def test_every_region_listed_means_everywhere(self):
        regions = ["United States", "Canada", "Europe", "Middle East", "Asia-Pacific"]
        self.assertEqual(self.clean(self.proposal(regions=regions), regions=regions)["regions"], [])

    def test_a_regional_page_keeps_its_regions_when_none_named(self):
        self.assertEqual(self.clean(self.proposal(regions=[]), default=["Europe"])["regions"], ["Europe"])

    def test_bad_values_fall_back_to_safe_ones(self):
        row = self.clean(self.proposal(severity="EXTREME", traffic_light="PURPLE", evaluation_mode="x"))
        self.assertEqual((row["severity"], row["traffic_light"], row["evaluation_mode"]), ("HIGH", "YELLOW", "CONTEXT"))

    def test_a_proposal_without_a_title_is_skipped(self):
        self.assertIsNone(self.clean(self.proposal(title=" ")))


class SimilarityTests(unittest.TestCase):
    def test_the_model_named_rule_comes_first_and_unknown_numbers_are_ignored(self):
        out = svc.merge_similar(
            [{"rule_number": "SMCP-1", "overlap": "DUPLICATE", "reason": "Same requirement"},
             {"rule_number": "Rule 999", "overlap": "PARTIAL", "reason": "made up"}],
            "Disclose paid promotions sponsored by", RULES,
        )
        self.assertEqual([(s.rule_number, s.overlap) for s in out][0], ("SMCP-1", "DUPLICATE"))
        self.assertNotIn("Rule 999", [s.rule_number for s in out])

    def test_strongly_similar_wording_is_found_without_the_model(self):
        out = svc.merge_similar([], "Disclose paid promotions and sponsorships with the paid promotion label", RULES)
        self.assertEqual([s.rule_number for s in out], ["SMCP-1"])

    def test_unrelated_wording_is_not_similar(self):
        self.assertEqual(svc.merge_similar([], "Music must be licensed for commercial use", RULES), [])


def pending(**over):
    p = dict(id=uuid4(), page_id=uuid4(), url="https://example.com/policy", status="PENDING", title="Disclose paid promotions",
             summary="Mark them.", severity="HIGH", traffic_light="YELLOW", evaluation_mode="CONTEXT",
             platforms=["YOUTUBE"], regions=[], paid_ads_only=True, source_quote="Quote here.",
             created_at=datetime.now(timezone.utc))
    p.update(over)
    return PolicyRuleProposal(**p)


class ApproveTests(unittest.IsolatedAsyncioTestCase):
    async def test_approving_creates_a_scoped_rule_in_the_policy_rulebook(self):
        p = pending()
        rule = type("Rule", (), {"id": uuid4(), "rule_number": "SMCP-3"})()
        create = AsyncMock(return_value=rule)
        pool = AsyncMock()
        pool.fetchval = AsyncMock(return_value=p.id)
        conn = AsyncMock()
        conn.fetchval = AsyncMock(return_value=3)
        pool.acquire = lambda: _Ctx(conn)
        book = uuid4()
        with (
            patch.object(svc, "_get_proposal", new=AsyncMock(return_value=p)),
            patch.object(svc, "rulebook_id", new=AsyncMock(return_value=book)),
            patch.object(svc, "get_pool", return_value=pool),
            patch.object(svc, "log_audit_event", new=AsyncMock()),
            patch("services.compliance.evaluation.compliance_service.create_compliance_rule", new=create),
            patch("services.compliance.submissions.submission_service.schedule_rulebook_recheck"),
        ):
            await svc.approve(p.id, uuid4())
        data = create.await_args.args[0]
        self.assertEqual((data.rule_number, data.category, data.rulebook_id), ("SMCP-3", svc.POLICY_CATEGORY, book))
        self.assertEqual((data.platforms, data.regions, data.rule_sets), (["YOUTUBE"], [], []))
        self.assertTrue(data.summary.startswith("Paid ads only."))
        self.assertIn("https://example.com/policy", data.remediation_guidance)

    async def test_a_decided_proposal_cant_be_decided_again(self):
        with patch.object(svc, "_get_proposal", new=AsyncMock(return_value=pending(status="APPROVED"))):
            with self.assertRaises(HTTPException) as e:
                await svc.reject(uuid4(), uuid4())
        self.assertEqual(e.exception.status_code, 409)


class _Ctx:
    def __init__(self, conn):
        self.conn = conn

    async def __aenter__(self):
        return self.conn

    async def __aexit__(self, *exc):
        return False


if __name__ == "__main__":
    unittest.main()
