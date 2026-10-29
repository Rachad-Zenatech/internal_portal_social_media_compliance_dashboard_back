import unittest

from services import post_signals as ps


class ExtractTests(unittest.TestCase):
    def test_cashtags_hashtags_mentions_and_links_are_read(self):
        s = ps.extract(
            "Big week for $ZENA! Thanks @USArmy and @ZenaDroneDefense. #DroneTech #ZenaTech "
            "Details: https://www.zenatech.com/news/q3 and army.mil/article/123. Email info@zenatech.com"
        )
        self.assertEqual(s.cashtags, ["$ZENA"])
        self.assertEqual(s.hashtags, ["#DroneTech", "#ZenaTech"])
        self.assertEqual(s.mentions, ["@USArmy", "@ZenaDroneDefense"])
        self.assertEqual(s.links, ["zenatech.com", "army.mil"])
        self.assertEqual(s.government_references, ["@USArmy", "army.mil"])
        self.assertEqual(s.outside_links, [])

    def test_money_and_numbers_are_not_cashtags_or_hashtags(self):
        s = ps.extract("A $25M contract, $10.5 million, ranked #1 in the category.")
        self.assertEqual(s.cashtags, [])
        self.assertEqual(s.hashtags, [])

    def test_an_email_address_is_not_a_mention_or_a_link(self):
        s = ps.extract("Write to press@zenatech.com for details.")
        self.assertEqual(s.mentions, [])
        self.assertEqual(s.links, [])

    def test_outside_links_are_listed_and_noted(self):
        s = ps.extract("Read more at https://defensenews.com/story and zenatech.com")
        self.assertEqual(s.outside_links, ["defensenews.com"])
        self.assertTrue(any("defensenews.com" in n for n in s.notes()))

    def test_a_hashtag_wall_is_noted(self):
        s = ps.extract("Launch day #a1 #b2 #c3 #d4 #e5 #f6")
        self.assertEqual(len(s.hashtags), 6)
        self.assertTrue(any("6 hashtags" in n for n in s.notes()))
        self.assertEqual(ps.extract("Launch day #a1 #b2 #c3").notes(), [])


class HashtagWordTests(unittest.TestCase):
    def test_camel_case_hashtags_split_into_words(self):
        self.assertEqual(ps.hashtag_words("CombatProven"), "combat proven")
        self.assertEqual(ps.hashtag_words("UAVTech"), "uav tech")
        self.assertEqual(ps.hashtag_words("combat_proven"), "combat proven")
        # One case throughout can't be split.
        self.assertEqual(ps.hashtag_words("combatproven"), "combatproven")

    def test_phrases_and_keys(self):
        s = ps.extract("#CombatProven #combatready #Drones")
        self.assertEqual(s.hashtag_phrases, ["combat proven"])
        self.assertEqual(s.hashtag_keys, ["combatproven", "combatready", "drones"])


class GovernmentTests(unittest.TestCase):
    def test_government_and_military_handles(self):
        for handle in ["USArmy", "DeptofDefense", "DeptofWar", "USArmyReserve", "CanadianForces", "NATO", "FAANews"]:
            self.assertTrue(ps.is_government_handle(handle), handle)

    def test_lookalikes_and_our_own_accounts_are_not(self):
        for handle in ["SalvationArmy", "OldNavy", "NavyFederal", "ZenaDroneDefense", "DefenseNews", "dronelife"]:
            self.assertFalse(ps.is_government_handle(handle), handle)

    def test_government_domains(self):
        for domain in ["army.mil", "defense.gov", "canada.gc.ca", "gov.uk", "nato.int"]:
            self.assertTrue(ps.is_government_domain(domain), domain)
        self.assertFalse(ps.is_government_domain("defensenews.com"))

    def test_tagging_the_military_is_a_finding(self):
        found = ps.findings(ps.extract("Proud to support @USArmy at the expo."))
        self.assertEqual([f.rule_id for f in found], [ps.GOVERNMENT_REFERENCE_ID])
        self.assertEqual(found[0].flagged_terms, ["@USArmy"])
        self.assertEqual(found[0].traffic_light, "YELLOW")

    def test_the_no_endorsement_wording_clears_it(self):
        text = ("Proud to support @USArmy at the expo. The appearance of U.S. Department of Defense visual "
                "information does not imply or constitute DOD endorsement.")
        self.assertEqual(ps.findings(ps.extract(text)), [])

    def test_not_only_is_not_a_disclaimer(self):
        self.assertTrue(ps.findings(ps.extract("Not only did @USArmy endorse it, they bought it.")))

    def test_ordinary_posts_have_no_findings(self):
        for text in [
            "Happy Thanksgiving from the ZenaTech family! #Thanksgiving #ZenaTech",
            "Meet the team at @CES this week. Booth 4412. zenatech.com",
        ]:
            self.assertEqual(ps.findings(ps.extract(text)), [], text)


if __name__ == "__main__":
    unittest.main()
