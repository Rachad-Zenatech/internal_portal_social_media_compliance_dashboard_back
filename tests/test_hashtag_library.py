import unittest
import uuid
from datetime import datetime, timezone

from models.compliance_model import ComplianceRule
from services.compliance import hashtag_library as hl
from services.compliance.compliance_service import flagged_tags


def rule(**fields):
    now = datetime.now(timezone.utc)
    data = {"id": uuid.uuid4(), "rule_number": "Rule X", "category": "Test", "title": "Test", "summary": "Test",
            "created_at": now, "updated_at": now}
    data.update(fields)
    return ComplianceRule(**data)


class LibraryTests(unittest.TestCase):
    def test_brand_and_audience_tags_with_the_cashtag_on_its_own(self):
        lib = hl.build("ZENADRONE", "DEFENSE", ["LINKEDIN"])
        labels = [g.label for g in lib.groups]
        self.assertEqual(labels, ["Brand", "For Defense & Government", "Investors"])
        self.assertIn("#ZenaDrone", [t.tag for t in lib.groups[0].tags])
        # Tags already offered under the brand aren't repeated under the audience.
        self.assertNotIn("#UAS", [t.tag for t in lib.groups[1].tags])
        cashtag = lib.groups[-1].tags[0]
        self.assertEqual(cashtag.tag, "$ZENA")
        self.assertIn("investor", cashtag.note.lower())

    def test_the_strictest_platform_sets_the_limit(self):
        self.assertEqual(hl.max_hashtags(["INSTAGRAM", "X"]), 2)
        self.assertEqual(hl.max_reason(["INSTAGRAM", "X"]), "Up to 2 for X")
        self.assertEqual(hl.max_hashtags([]), hl.DEFAULT_MAX)
        self.assertEqual(hl.max_hashtags(["REDDIT"]), 0)
        self.assertEqual(hl.max_reason(["REDDIT"]), "No hashtags on Reddit")

    def test_only_allowed_tags_are_offered(self):
        lib = hl.build("ZENATECH", None, [], allowed=["#ZenaTech"])
        self.assertEqual([t.tag for t in lib.groups[0].tags], ["#ZenaTech"])

    def test_an_unknown_brand_gets_the_company_tags(self):
        self.assertIn("#ZenaTech", hl.candidates(None, None))


class FlaggedTagTests(unittest.TestCase):
    def test_a_tag_the_rules_flag_is_reported_with_its_rule(self):
        rules = [rule(rule_number="Rule 2", prohibited_terms=["combat proven"])]
        self.assertEqual(flagged_tags(["#CombatProven", "#combatproven", "#Drones"], rules),
                         {"#CombatProven": "Rule 2", "#combatproven": "Rule 2"})

    def test_a_disclaimer_rule_does_not_flag_a_tag(self):
        rules = [rule(required_disclaimers=["Not an endorsement"])]
        self.assertEqual(flagged_tags(["#ZenaTech"], rules), {})


if __name__ == "__main__":
    unittest.main()
