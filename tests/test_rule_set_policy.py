"""Brand and audience routing is enforced independently of saved or manually selected sets."""
import unittest
from datetime import datetime, timezone
from unittest.mock import AsyncMock, patch
from uuid import uuid4
from fastapi import HTTPException
from models.compliance.compliance_model import ComplianceCheckRequest, ComplianceRule
from services.compliance.evaluation import rule_set_policy as policy, compliance_service as cs
from services.compliance.submissions.source_link_service import apply_update_fields


BRANDS = {"ZENATECH": policy.GENERAL, "SOFTWARE": policy.GENERAL, "DAAS": policy.DEFENSE,
          "ZENADRONE": policy.DEFENSE, "CEO_THOUGHT_LEADERSHIP": policy.CEO}


def rule(number, rule_sets, **changes):
    now = datetime.now(timezone.utc)
    return ComplianceRule(id=uuid4(), rule_number=number, title=number, category="Test", summary="Test",
        rule_sets=rule_sets, created_at=now, updated_at=now, **changes)


class RuleSetPolicyTests(unittest.TestCase):
    def test_every_brand_and_audience_combination_replaces_stale_sets(self):
        for brand, own in BRANDS.items():
            for audience in ["GENERAL", "INVESTORS", "COMMERCIAL", "DEFENSE"]:
                with self.subTest(brand=brand, audience=audience):
                    expected = {policy.PUBLIC, own} | ({policy.DEFENSE} if audience == "DEFENSE" else set())
                    self.assertEqual(set(policy.resolve([brand], [audience], policy.MANAGED)), expected)
                    req = ComplianceCheckRequest(text="Ordinary post", brand_code=brand, audience=audience,
                                                 rule_sets=list(policy.MANAGED))
                    self.assertEqual(set(cs.check_scope(req).rule_sets), expected)

    def test_public_disclosure_is_present_even_before_a_brand_is_selected(self):
        self.assertEqual(cs.check_scope(ComplianceCheckRequest(text="plain")).rule_sets, [policy.PUBLIC])
        self.assertEqual(policy.resolve(["A_NEW_BRAND"]), [policy.PUBLIC])

    def test_defense_is_added_for_any_of_multiple_selected_audiences(self):
        self.assertEqual(policy.resolve(["SOFTWARE"], ["GENERAL", "DEFENSE"]),
                         [policy.PUBLIC, policy.GENERAL, policy.DEFENSE])

    def test_removing_defense_and_changing_brand_drops_stale_managed_sets(self):
        old = policy.resolve(["SOFTWARE"], ["DEFENSE"])
        self.assertEqual(policy.resolve(["CEO_THOUGHT_LEADERSHIP"], ["GENERAL"], old), [policy.PUBLIC, policy.CEO])
        self.assertEqual(policy.resolve(["SOFTWARE"], ["GENERAL"], old), [policy.PUBLIC, policy.GENERAL])

    def test_stale_ceo_rule_selection_does_not_classify_a_software_post_as_ceo_content(self):
        scope = cs.check_scope(ComplianceCheckRequest(text="plain", brand_code="SOFTWARE", rule_sets=[policy.CEO]))
        self.assertNotIn("ceo_post", scope.content_parts)

    def test_explicit_ad_rule_set_is_preserved(self):
        self.assertEqual(policy.resolve(["SOFTWARE"], [], [*policy.MANAGED, "Paid Social Ads"]),
                         [policy.PUBLIC, policy.GENERAL, "Paid Social Ads"])

    def test_military_words_do_not_select_a_rule_set_for_another_brand(self):
        req = ComplianceCheckRequest(text="U.S. Army, Pentagon and military update.", brand_code="SOFTWARE", audience="GENERAL")
        self.assertEqual(cs.check_scope(req).rule_sets, [policy.PUBLIC, policy.GENERAL])

    def test_rule_level_audience_platform_and_brand_constraints_still_apply(self):
        limited = rule("Scoped", [policy.GENERAL], audiences=["COMMERCIAL"], platforms=["LINKEDIN"], brands=["SOFTWARE"])
        req = ComplianceCheckRequest(text="plain", brand_code="SOFTWARE", audience="COMMERCIAL", platforms=["LINKEDIN"])
        self.assertTrue(cs._rule_applies(limited, req))
        self.assertFalse(cs._rule_applies(limited, req.model_copy(update={"audiences": ["GENERAL"]})))
        self.assertFalse(cs._rule_applies(limited, req.model_copy(update={"platforms": ["FACEBOOK"]})))
        self.assertFalse(cs._rule_applies(limited, req.model_copy(update={"brand_codes": ["ZENATECH"]})))


class DraftPolicyTests(unittest.TestCase):
    def test_saved_brand_change_replaces_legacy_rule_selection(self):
        row = {"brand_code": "ZENADRONE", "audience": "GENERAL", "rule_sets": [policy.PUBLIC, policy.DEFENSE]}
        fields = {"brand_codes": ["SOFTWARE"]}
        apply_update_fields({}, fields, policy.MANAGED, row=row)
        self.assertEqual(fields["rule_sets"], [policy.PUBLIC, policy.GENERAL])

    def test_saved_audience_changes_update_sets_even_without_rule_sets_in_patch(self):
        row = {"brand_codes": ["SOFTWARE"], "audiences": ["GENERAL"], "rule_sets": []}
        fields = {"audiences": ["DEFENSE"]}
        apply_update_fields({}, fields, policy.MANAGED, row=row)
        self.assertEqual(fields["rule_sets"], [policy.PUBLIC, policy.GENERAL, policy.DEFENSE])
        row.update(fields)
        fields = {"audiences": ["GENERAL"]}
        apply_update_fields({}, fields, policy.MANAGED, row=row)
        self.assertEqual(fields["rule_sets"], [policy.PUBLIC, policy.GENERAL])

    def test_required_sets_cannot_be_removed_by_direct_api_patch(self):
        row = {"brand_codes": ["DAAS"], "audiences": ["GENERAL"]}
        fields = {}
        apply_update_fields({"rule_sets": []}, fields, policy.MANAGED, row=row)
        self.assertEqual(fields["rule_sets"], [policy.PUBLIC, policy.DEFENSE])

    def test_unknown_sets_are_still_rejected(self):
        with self.assertRaises(HTTPException) as raised:
            apply_update_fields({"rule_sets": ["Unknown"]}, {}, policy.MANAGED, row={})
        self.assertEqual(raised.exception.status_code, 422)


class ApplicablePolicyTests(unittest.IsolatedAsyncioTestCase):
    async def test_military_mention_and_ai_flag_do_not_bypass_selected_rule_sets(self):
        rows = [rule("Rule 4", [policy.DEFENSE]), rule("Rule 9", [policy.DEFENSE]),
                rule("Public", [policy.PUBLIC]), rule("General", [policy.GENERAL]), rule("Universal", [])]
        with patch.object(cs, "list_compliance_rules", AsyncMock(return_value=rows)):
            req = ComplianceCheckRequest(text="U.S. Army update", brand_code="SOFTWARE", audience="GENERAL", ai_generated_images=True)
            self.assertEqual([r.rule_number for r in await cs.applicable_rules(req)], ["Public", "General", "Universal"])
            req = ComplianceCheckRequest(text="plain", brand_code="SOFTWARE", audience="DEFENSE")
            self.assertEqual([r.rule_number for r in await cs.applicable_rules(req)], [r.rule_number for r in rows])

    async def test_drone_and_thought_leadership_get_distinct_rule_sets(self):
        rows = [rule("Public", [policy.PUBLIC]), rule("Defense", [policy.DEFENSE]), rule("CEO", [policy.CEO]), rule("General", [policy.GENERAL])]
        with patch.object(cs, "list_compliance_rules", AsyncMock(return_value=rows)):
            for brand, own in [("DAAS", "Defense"), ("ZENADRONE", "Defense"), ("CEO_THOUGHT_LEADERSHIP", "CEO")]:
                with self.subTest(brand=brand):
                    req = ComplianceCheckRequest(text="plain", brand_code=brand, audience="GENERAL")
                    self.assertEqual([r.rule_number for r in await cs.applicable_rules(req)], ["Public", own])
