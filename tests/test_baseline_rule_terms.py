"""Every seeded prohibited term must be enforceable by its own baseline rule."""
import unittest
from datetime import datetime, timezone
from uuid import uuid4

from models.compliance.compliance_model import ComplianceRule
from postgresql_db.schemas.setup_compliance_schema import BASE_RULES, _CONTEXTUAL_BASELINE
from services.compliance.evaluation.compliance_service import _evaluate_rule, _normalize


def rule_from_seed(seed):
    now = datetime.now(timezone.utc)
    configured = dict(seed)
    guidance = _CONTEXTUAL_BASELINE.get(seed["rule_number"])
    if guidance:
        configured.update(evaluation_mode="CONTEXT", evaluation_guidance=guidance)
    return ComplianceRule(id=uuid4(), created_at=now, updated_at=now, **configured)


class BaselineRuleTermTests(unittest.TestCase):
    def test_every_seeded_rule_has_a_testable_enforcement_path(self):
        for seed in BASE_RULES:
            rule = rule_from_seed(seed)
            with self.subTest(rule=rule.rule_number):
                trigger = (rule.prohibited_terms or rule.disclaimer_triggers or [""])[0]
                finding = _evaluate_rule(rule, f" {_normalize(trigger)} ")
                if rule.prohibited_terms or rule.required_disclaimers:
                    self.assertIsNotNone(finding)
                else:
                    # Image/object-only policy language is advisory until the rule schema has a detector field.
                    self.assertEqual(rule.evaluation_mode, "CONTEXT")
                    self.assertTrue(rule.evaluation_guidance)

    def test_every_seeded_prohibited_term_flags_its_rule(self):
        for seed in BASE_RULES:
            rule = rule_from_seed(seed)
            for term in rule.prohibited_terms:
                with self.subTest(rule=rule.rule_number, term=term):
                    finding = _evaluate_rule(rule, f" {_normalize(term)} ")
                    self.assertIsNotNone(finding)
                    self.assertIn(term, finding.flagged_terms)


if __name__ == "__main__":
    unittest.main()
