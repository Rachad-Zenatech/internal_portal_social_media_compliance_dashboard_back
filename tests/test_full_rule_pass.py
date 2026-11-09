import json
import unittest
from datetime import datetime, timezone
from unittest.mock import AsyncMock, patch
from uuid import uuid4

from models.compliance.compliance_model import ComplianceCheckRequest, ComplianceRule
from services.compliance import compliance_service as cs


def rule(number, severity="MEDIUM"):
    now = datetime.now(timezone.utc)
    return ComplianceRule(
        id=uuid4(), rule_number=number, category="Claims", title=f"{number} title", summary="s", created_at=now,
        updated_at=now, severity=severity, traffic_light="YELLOW",
    )


RULES = [rule("Rule 16"), rule("Rule 24"), rule("Rule 25")]
TEXT = "ZenaDrone's swarm system autonomously guards every border."


class FullRulePassTests(unittest.IsolatedAsyncioTestCase):
    async def run_pass(self, vector_rules, answer, full=True):
        seen = {}

        async def fake_generate(client, prompt, config, feature=None, **_):
            seen["prompt"] = prompt
            return type("Resp", (), {"text": json.dumps({"semantic_violations": answer})})()

        await cs._ai_check_cache.clear()
        with (
            patch.object(cs, "FULL_RULE_PASS", full),
            patch.object(cs, "_vector_context_for_check", new=AsyncMock(return_value=(vector_rules, []))),
            patch.object(cs, "generate_content_with_fallback", new=fake_generate),
            patch.dict("os.environ", {"GEMINI_API_KEY": "test-key"}),
        ):
            found = await cs._semantic_findings(ComplianceCheckRequest(text=TEXT), RULES, set(), cs._normalize(TEXT))
        return found, seen.get("prompt")

    async def test_every_applicable_rule_is_put_to_the_ai_even_when_the_search_found_none(self):
        found, prompt = await self.run_pass([], [{
            "rule_number": "Rule 16", "quote": "autonomously guards every border",
            "explanation": "States a vision as a current capability.", "confidence": 0.9,
        }])
        self.assertIsNotNone(prompt)
        for number in ("Rule 16", "Rule 24", "Rule 25"):
            self.assertIn(number, prompt)
        self.assertEqual([v.rule_number for v in found], ["Rule 16"])

    async def test_a_rule_the_wording_already_caught_is_not_asked_again(self):
        await cs._ai_check_cache.clear()
        asked = {}

        async def fake_generate(client, prompt, config, feature=None, **_):
            asked["prompt"] = prompt
            return type("Resp", (), {"text": '{"semantic_violations": []}'})()

        with (
            patch.object(cs, "_vector_context_for_check", new=AsyncMock(return_value=([], []))),
            patch.object(cs, "generate_content_with_fallback", new=fake_generate),
            patch.dict("os.environ", {"GEMINI_API_KEY": "test-key"}),
        ):
            await cs._semantic_findings(ComplianceCheckRequest(text=TEXT), RULES, {"Rule 24"}, cs._normalize(TEXT))
        self.assertNotIn('"rule_number": "Rule 24"', asked["prompt"])
        self.assertIn('"rule_number": "Rule 16"', asked["prompt"])

    async def test_switched_off_it_asks_nothing_when_the_search_found_nothing(self):
        found, prompt = await self.run_pass([], [], full=False)
        self.assertIsNone(prompt)
        self.assertEqual(found, [])

    async def test_the_prompt_asks_for_soft_claims(self):
        _, prompt = await self.run_pass([], [])
        self.assertIn("soft claims", prompt)
        self.assertIn("superlative", prompt)


if __name__ == "__main__":
    unittest.main()
