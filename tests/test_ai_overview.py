import datetime as dt
import unittest
from unittest.mock import AsyncMock, patch
from uuid import uuid4

from services.ai.observability import ai_health, ai_overview_service


class TestAiOverviewService(unittest.IsolatedAsyncioTestCase):
    async def test_overview_combines_outages_cores_and_usage(self):
        now = dt.datetime.now(dt.timezone.utc)
        fake_health = ai_health.AiStatus(
            available=True,
            degraded_features=[
                ai_health.FeatureIssue(
                    feature="EMBEDDINGS",
                    label="Vector Search & Embeddings",
                    status="OUT",
                    reason="KEY",
                    message="Access denied (~15m pause; using keyword fallback)",
                    since=now,
                ),
                ai_health.FeatureIssue(
                    feature="SOURCE_CHECK",
                    label="Source Link Fact-Check",
                    status="OVERFLOW",
                    reason="QUOTA",
                    message="The AI limit has been reached",
                    since=now,
                ),
            ],
        )
        feature_rows = [
            {
                "feature": "COMPLIANCE_CHECK",
                "calls": 12,
                "errors": 0,
                "blocked": 0,
                "cache_hits": 2,
                "input_tokens": 5000,
                "output_tokens": 1200,
                "avg_latency_ms": 840.5,
                "p95_latency_ms": 1400.0,
                "models_used": ["us.anthropic.claude-haiku-5-5"],
                "last_call_at": now,
                "last_status": "OK",
                "last_error_type": None,
            }
        ]
        usage = {
            "PUBLIC_MOOD": {"used": 1, "limit": 0, "company_used": 4, "company_limit": 0, "remaining": None}
        }

        with patch.object(ai_health, "status", AsyncMock(return_value=fake_health)), \
             patch.object(ai_overview_service, "fetch_all", AsyncMock(side_effect=[feature_rows, feature_rows, [], []])), \
             patch.object(ai_overview_service.ai_usage_service, "usage_for", AsyncMock(return_value=usage)), \
             patch("services.ai.core.bedrock_service.is_bedrock_enabled", return_value=True), \
             patch("services.ai.core.bedrock_service.has_ai_key", return_value=True), \
             patch("services.ai.core.bedrock_service.get_bedrock_profile_id", return_value="us.anthropic.claude-haiku-5-5"), \
             patch("services.ai.core.bedrock_service.get_bedrock_region", return_value="us-east-1"), \
             patch("services.ai.core.vector_store_service.is_enabled", return_value=True), \
             patch("services.ai.core.vector_store_service.embedding_pause_status", return_value=("Access denied", 900)):
            result = await ai_overview_service.overview(hours=24, user_id=uuid4())

        self.assertEqual(result["totals"]["calls"], 12)
        self.assertEqual(result["totals"]["features_out"], 1)
        self.assertEqual(result["totals"]["features_overflow"], 1)

        by_feat = {f["feature"]: f for f in result["features"]}
        self.assertEqual(by_feat["COMPLIANCE_CHECK"]["status"], "OK")
        self.assertEqual(by_feat["COMPLIANCE_CHECK"]["primary_core"], "bedrock_llm")
        self.assertEqual(by_feat["EMBEDDINGS"]["status"], "OUT")
        self.assertEqual(by_feat["SOURCE_CHECK"]["status"], "OVERFLOW")
        self.assertEqual(by_feat["CAPTION"]["status"], "READY")

        by_core = {c["core"]: c for c in result["cores"]}
        self.assertEqual(by_core["titan_embeddings"]["status"], "OUT")
        self.assertIn("COMPLIANCE_CHECK", by_core["bedrock_llm"]["features"])


if __name__ == "__main__":
    unittest.main()
