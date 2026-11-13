"""Source trust: what reviewers' calls on a site add up to, and how it orders the sources found. No database."""

import unittest
from unittest.mock import AsyncMock, patch

from services.compliance.submissions import source_trust_service as st


class StandingTests(unittest.TestCase):
    def test_a_site_reviewers_rated_good_is_trusted(self):
        s = st._standing("aerotime.aero", {"good": 2, "poor": 0, "confirmations": 3, "matches": 3})
        self.assertEqual(s.level, "TRUSTED")
        self.assertIn("rated it a good source 2 times", s.message)

    def test_a_site_rated_poor_more_often_than_good_is_flagged(self):
        self.assertEqual(st._standing("blog.example", {"good": 1, "poor": 2}).level, "FLAGGED")

    def test_enough_checked_claims_without_ratings_count_as_trusted(self):
        self.assertEqual(st._standing("x.example", {"confirmations": st.TRUST_MIN_CONFIRMATIONS}).level, "TRUSTED")
        self.assertEqual(st._standing("x.example", {"confirmations": 1}).level, "UNRATED")

    def test_mismatches_dont_count_against_a_site(self):
        # Reviewers found the post wrong against this source three times: the source did its job.
        s = st._standing("newswire.ca", {"confirmations": 3, "matches": 0})
        self.assertEqual(s.level, "TRUSTED")

    def test_a_site_nobody_has_called_is_unrated(self):
        self.assertEqual(st._standing("new.example", {}).level, "UNRATED")


class KeyTests(unittest.TestCase):
    def test_sites_ignore_www_and_case(self):
        self.assertEqual(st.host_of("https://WWW.Aerotime.aero/articles/x"), "aerotime.aero")
        self.assertEqual(st.host_of("newswire.ca/news"), "newswire.ca")

    def test_the_same_claim_worded_with_other_spacing_or_case_is_one_claim(self):
        self.assertEqual(st.claim_key("CAD $8.6M  Q2 DaaS revenue"), st.claim_key("cad $8.6m q2 daas revenue!"))
        self.assertNotEqual(st.claim_key("CAD $8.6M"), st.claim_key("CAD $8.1M"))
        self.assertEqual(st.claim_key(""), "")


class RankTests(unittest.IsolatedAsyncioTestCase):
    async def test_trusted_sites_first_and_flagged_last(self):
        urls = ["https://flagged.example/a", "https://unrated.example/b", "https://trusted.example/c"]
        trust = {
            "flagged.example": st._standing("flagged.example", {"poor": 2}),
            "unrated.example": st._standing("unrated.example", {}),
            "trusted.example": st._standing("trusted.example", {"good": 1}),
        }
        with patch.object(st, "trust_for", new=AsyncMock(return_value=trust)):
            ranked = await st.rank_by_trust(urls)
        self.assertEqual(ranked, ["https://trusted.example/c", "https://unrated.example/b", "https://flagged.example/a"])

    async def test_without_a_database_the_order_is_kept(self):
        with patch.object(st, "trust_for", new=AsyncMock(side_effect=RuntimeError("no pool"))):
            self.assertEqual(await st.rank_by_trust(["b", "a"]), ["b", "a"])


class WhoCountsTests(unittest.IsolatedAsyncioTestCase):
    """Calls count when the person decides on the post: reviewer, delegate, or final approver (or their delegate)."""

    def setUp(self):
        from uuid import uuid4

        self.author, self.someone = uuid4(), uuid4()
        self.row = {"id": uuid4(), "created_by": self.author, "reviewer_ids": []}

    async def counts(self, user, *, reviewer=False, super_admin=False, approvers=(), delegates=()):
        from services.compliance.submissions import approval_path_service, delegation_service
        from services.compliance.submissions import submission_service as subs

        rules = unittest.mock.MagicMock()
        with patch.object(subs, "_is_super_admin", new=AsyncMock(return_value=super_admin)), \
                patch.object(subs, "_can_review", new=AsyncMock(return_value=reviewer)), \
                patch.object(subs, "_approvers", return_value=(list(approvers), [])), \
                patch.object(subs, "_accounts", return_value=[]), \
                patch.object(approval_path_service, "load_rules", new=AsyncMock(return_value=rules)), \
                patch.object(delegation_service, "with_delegates",
                             new=AsyncMock(return_value=list(approvers) + list(delegates))):
            return await st.counts_toward_trust(self.row, user)

    async def test_a_reviewer_counts(self):
        self.assertTrue(await self.counts(self.someone, reviewer=True))

    async def test_someone_the_post_was_delegated_to_counts_without_the_reviewer_role(self):
        self.row["reviewer_ids"] = [self.someone]
        self.assertTrue(await self.counts(self.someone))

    async def test_a_named_final_approver_and_their_delegate_count(self):
        self.assertTrue(await self.counts(self.someone, approvers=[self.someone]))
        delegate = __import__("uuid").uuid4()
        self.assertTrue(await self.counts(delegate, approvers=[self.someone], delegates=[delegate]))

    async def test_the_author_does_not_count_even_as_a_reviewer(self):
        self.assertFalse(await self.counts(self.author, reviewer=True))

    async def test_anyone_else_does_not_count(self):
        self.assertFalse(await self.counts(self.someone))

    async def test_a_super_admin_counts(self):
        self.assertTrue(await self.counts(self.author, super_admin=True))


if __name__ == "__main__":
    unittest.main()
