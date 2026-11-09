import unittest

from postgresql_db.schemas import setup_compliance_schema as schema


class ForwardLookingGuidanceTests(unittest.TestCase):
    def test_rule_25_tells_the_ai_generic_enthusiasm_is_not_forward_looking(self):
        guidance = schema._ADDED_GUIDANCE["Rule 25"]
        self.assertIn("Exciting things ahead", guidance)
        self.assertIn("report nothing", guidance)

    def test_rule_25_still_requires_the_safe_harbor_for_real_projections(self):
        guidance = schema._ADDED_GUIDANCE["Rule 25"]
        for real in ("projection", "forecast", "milestone", "commitment"):
            self.assertIn(real, guidance)
        self.assertIn("safe-harbor", guidance)

    def test_guidance_is_only_added_where_a_rule_has_none(self):
        import inspect

        self.assertIn("evaluation_guidance IS NULL", inspect.getsource(schema._add_baseline_guidance))


if __name__ == "__main__":
    unittest.main()
