"""Verify AI token-reduction budgets, compact schemas, rule deduplication, and visual cache hits."""

import json
import unittest
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

from botocore.exceptions import ClientError

from services.ai.core import bedrock_service, model_router
from services.compliance.analytics import public_mood_prompt
from services.compliance.evaluation import context_evaluator, visual_review


class TestTokenBudgets(unittest.IsolatedAsyncioTestCase):
    def test_model_router_prefers_cheap_for_easy_and_stronger_when_needed(self):
        with patch.dict("os.environ", {"AWS_PROFILE_ID": "us.anthropic.claude-3-5-sonnet-20241022-v2:0"}, clear=True):
            easy = model_router.route("IMAGE_TEXT", attachments=1)
            self.assertEqual(easy.tier, "LIGHT")
            self.assertEqual(easy.models[0], "us.anthropic.claude-haiku-5-5")

            ctx = model_router.route("CONTEXT_EVAL", prompt_chars=500)
            self.assertEqual(ctx.tier, "LIGHT")
            self.assertEqual(ctx.models[0], "us.anthropic.claude-haiku-5-5")

            heavy = model_router.route("SENTIMENT", high_stakes=True)
            self.assertEqual(heavy.tier, "HEAVY")
            self.assertEqual(heavy.models[0], "us.anthropic.claude-3-5-sonnet-20241022-v2:0")

        with patch.dict("os.environ", {"AWS_PROFILE_ID": "us.anthropic.claude-haiku-5-5"}, clear=True):
            heavy = model_router.route("COMPLIANCE_CHECK", prompt_chars=70_000)
            self.assertEqual(heavy.tier, "HEAVY")
            self.assertEqual(heavy.models[0], "us.anthropic.claude-3-5-sonnet-20241022-v2:0")
            self.assertIn("us.anthropic.claude-haiku-5-5", heavy.models)

    def test_bedrock_adapter_falls_back_to_profile_on_access_denied(self):
        adapter = bedrock_service.BedrockClientAdapter()
        mock_boto = MagicMock()
        mock_boto.converse.side_effect = [
            ClientError({"Error": {"Code": "AccessDeniedException", "Message": "denied"}}, "Converse"),
            {"output": {"message": {"content": [{"text": "ok"}]}}, "usage": {"inputTokens": 5, "outputTokens": 2}},
        ]
        adapter._boto_client = mock_boto
        with patch.dict("os.environ", {"AWS_PROFILE_ID": "us.anthropic.claude-haiku-5-5"}, clear=True):
            resp = adapter.models.generate_content(
                model="us.anthropic.claude-3-5-sonnet-20241022-v2:0",
                contents="hello",
            )
        self.assertEqual(resp.text, "ok")
        self.assertEqual(mock_boto.converse.call_count, 2)
        self.assertEqual(
            mock_boto.converse.call_args_list[1].kwargs["modelId"],
            "us.anthropic.claude-haiku-5-5",
        )
    def test_bedrock_schema_compact_and_default_max_tokens(self):
        cfg = SimpleNamespace(
            system_instruction="Judge rules.",
            response_mime_type="application/json",
            response_schema={"type": "OBJECT", "properties": {"ok": {"type": "BOOLEAN"}}},
        )
        params = bedrock_service._build_converse_params("us.anthropic.claude-haiku-5-5", "hello", cfg)
        self.assertEqual(params["inferenceConfig"]["maxTokens"], 2048)
        sys_text = params["system"][0]["text"]
        self.assertIn('{"type":"OBJECT","properties":{"ok":{"type":"BOOLEAN"}}}', sys_text)

    def test_context_evaluator_deduplicates_rules_per_batch(self):
        rule = SimpleNamespace(
            id=uuid4(),
            rule_number="Rule 4",
            title="Government Endorsement",
            severity="HIGH",
            summary="No endorsement implication.",
            evaluation_guidance="Require affirmative claim.",
            exception_terms=[],
            allowed_examples=[],
            prohibited_examples=[],
            required_disclaimers=[],
            updated_at=datetime.now(timezone.utc),
        )
        f1 = SimpleNamespace(kind="PROHIBITED_TERM", flagged_terms=["Army"], status="VIOLATION")
        f2 = SimpleNamespace(kind="PROHIBITED_TERM", flagged_terms=["Pentagon"], status="VIOLATION")
        batch = [
            (0, context_evaluator.Candidate(finding=f1, rule=rule, passage="p1", context="c1")),
            (1, context_evaluator.Candidate(finding=f2, rule=rule, passage="p2", context="c2")),
        ]
        payload = context_evaluator._batch_payload(batch)
        self.assertEqual(list(payload["rules"].keys()), ["Rule 4"])
        self.assertEqual(len(payload["items"]), 2)
        self.assertNotIn("rule", payload["items"][0])
        self.assertEqual(payload["items"][0]["rule_number"], "Rule 4")

    def test_public_mood_prompt_caps_and_compacts_sample_posts(self):
        posts = [SimpleNamespace(sentiment="POSITIVE", confidence=0.9, text="x" * 400) for _ in range(8)]
        snap = SimpleNamespace(
            fetched_at=datetime.now(timezone.utc),
            window_days=7,
            positive_pct=80.0,
            neutral_pct=15.0,
            negative_pct=5.0,
            volume=10,
            volume_change_pct=5.0,
            is_spike=False,
            recommendation="POST",
            recommendation_reason="Positive coverage",
            top_posts=posts,
        )
        topic = SimpleNamespace(name="ZenaDrone", matched_terms=["ZenaDrone"], snapshot=snap)
        mood = SimpleNamespace(topics=[topic], source="x_json", is_mock=False)
        prompt = public_mood_prompt.build_summary_prompt(
            {"brand_name": "ZenaTech", "brand_code": "ZENATECH", "audience": "GENERAL", "platforms": ["X"], "text": "Launch"},
            mood,
            lambda _: ("POST", "Positive"),
        )
        json_part = prompt.split("LISTENING DATA (x_json)\n", 1)[1].strip()
        parsed = json.loads(json_part)
        self.assertEqual(len(parsed[0]["sample_posts"]), 4)
        self.assertEqual(len(parsed[0]["sample_posts"][0]["text"]), 240)

    async def test_visual_review_caches_unchanged_assets(self):
        await visual_review._VISUAL_CACHE.clear()
        rule = SimpleNamespace(
            id=uuid4(),
            rule_number="Rule 11",
            title="Official Seals",
            category="VISUAL",
            severity="HIGH",
            traffic_light="YELLOW",
            summary="No DoD seals.",
            evaluation_guidance="",
            exception_terms=[],
            allowed_examples=[],
            prohibited_examples=[],
            remediation_guidance="",
            updated_at=datetime.now(timezone.utc),
        )
        asset = {"id": uuid4(), "original_name": "img.jpg", "mime_type": "image/jpeg", "storage_path": "a/img.jpg"}
        req = SimpleNamespace(
            text="Hello",
            disclaimer_text="",
            audiences=["GENERAL"],
            brand_codes=["ZENATECH"],
            region="US",
            sources=[],
            _visual_media=[asset],
        )
        fake_response = SimpleNamespace(
            text=json.dumps({
                "assessments": [{
                    "rule_number": "Rule 11",
                    "status": "CLEARED",
                    "evidence_basis": "NO_CONCERN",
                    "confidence": 0.95,
                    "evidence": "",
                    "reasoning": "No official seal shown.",
                    "box": [],
                    "start_sec": 0,
                    "end_sec": 0,
                }]
            }),
            model_version="claude-test",
        )
        gen_mock = AsyncMock(return_value=fake_response)

        from contextlib import asynccontextmanager

        @asynccontextmanager
        async def fake_part(_asset):
            yield "part"

        with patch.dict("os.environ", {"CACHE_ENABLED": "true"}), \
             patch.object(visual_review, "_asset_part", fake_part), \
             patch.object(visual_review.image_text_service, "_generate", gen_mock), \
             patch("services.ai.core.bedrock_service.is_bedrock_enabled", return_value=True):
            await visual_review.review(req, [rule], [])
            await visual_review.review(req, [rule], [])

        self.assertEqual(gen_mock.await_count, 1)


if __name__ == "__main__":
    unittest.main()
