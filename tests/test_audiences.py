import unittest

from models.compliance.compliance_model import ComplianceCheckRequest
from services.compliance import hashtag_library as hl
from services.compliance.approval_path_service import ApprovalRules
from services.compliance.compliance_service import audiences_of, check_scope
from uuid import uuid4


class AudiencesTests(unittest.TestCase):
    def test_a_post_can_target_several_audiences_and_the_first_is_the_primary(self):
        req = ComplianceCheckRequest(text="x", audiences=["GENERAL", "DEFENSE"])
        self.assertEqual((req.audience, req.audiences), ("GENERAL", ["GENERAL", "DEFENSE"]))

    def test_a_single_audience_still_works(self):
        req = ComplianceCheckRequest(text="x", audience="INVESTORS")
        self.assertEqual(req.audiences, ["INVESTORS"])

    def test_rules_for_any_of_the_audiences_are_checked(self):
        scope = check_scope(ComplianceCheckRequest(text="plain", audiences=["GENERAL", "DEFENSE"]))
        self.assertTrue({"GENERAL", "DEFENSE"} <= set(scope.audiences))

    def test_no_audience_checks_every_rule(self):
        self.assertEqual(check_scope(ComplianceCheckRequest(text="plain")).audiences, [])

    def test_stored_rows_without_the_list_use_their_single_audience(self):
        self.assertEqual(audiences_of({"audience": "INVESTORS", "audiences": []}), ["INVESTORS"])
        self.assertEqual(audiences_of({"audience": "GENERAL", "audiences": ["GENERAL", "DEFENSE"]}), ["GENERAL", "DEFENSE"])
        self.assertEqual(audiences_of({"audience": None, "audiences": []}), [])

    def test_a_path_for_any_audience_of_the_post_gives_its_approvers(self):
        a, b = uuid4(), uuid4()
        rules = ApprovalRules([
            {"brand_code": None, "audience": "DEFENSE", "ceo_content": False, "approver_ids": [a], "approver_names": ["Ann"]},
            {"brand_code": None, "audience": "INVESTORS", "ceo_content": False, "approver_ids": [b], "approver_names": ["Bo"]},
        ])
        ids, names = rules.approvers("ZENATECH", ["GENERAL", "DEFENSE"], False)
        self.assertEqual((ids, names), ([a], ["Ann"]))
        ids, _ = rules.approvers("ZENATECH", ["DEFENSE", "INVESTORS"], False)
        self.assertEqual(set(ids), {a, b})

    def test_hashtags_from_every_audience_are_offered(self):
        both = set(hl.candidates("ZENATECH", ["DEFENSE", "INVESTORS"]))
        self.assertTrue(set(hl.candidates("ZENATECH", ["DEFENSE"])) <= both)
        self.assertTrue(set(hl.candidates("ZENATECH", ["INVESTORS"])) <= both)


if __name__ == "__main__":
    unittest.main()
