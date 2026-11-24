"""A model may omit a fact reference without invalidating the whole sentiment assessment."""
import json
import unittest
from copy import deepcopy
from unittest.mock import AsyncMock, patch

from pydantic import ValidationError

from models.compliance.sentiment_model import ClaimCheck
from services.ai.core.bedrock_service import BedrockResponse
from services.compliance.sentiment import engine
from services.compliance.sentiment.response_format import parse_response

RESPONSE = {
    "summary": "Informative post.", "overall_assessment": "REVIEW_SUGGESTED", "reputational_concern": "LOW",
    "tone": {"primary": "informative", "intensity": "LOW", "evidence": []},
    "brand_alignment": {"rating": "INSUFFICIENT_GUIDANCE", "explanation": "No brand voice was supplied."},
    "controversy": {"applies": False}, "findings": [],
    "context_report": {
        "brand_voice": {"verdict": "NO_GUIDE", "headline": "No guide", "summary": "No guide supplied."},
        "facts": {"verdict": "UNVERIFIED", "summary": "No matching fact.", "claims": [
            {"claim": "Drones can inspect buildings.", "result": "NO_FACT", "ref": None, "note": "No fact on file."},
        ]},
        "platform_norms": {"verdict": "NO_NORMS", "summary": "No norms supplied."},
        "audiences": {"verdict": "NO_PROFILES", "summary": "No profiles supplied."},
    },
}


class SentimentContextRefTests(unittest.TestCase):
    def test_null_reference_is_normalized_without_inventing_a_source(self):
        result = parse_response(BedrockResponse(json.dumps(RESPONSE)), 16384)
        claim = result.context_report.facts.claims[0]
        self.assertEqual(claim.ref, "")
        self.assertEqual(claim.result, "NO_FACT")
        self.assertIsNone(claim.entry_id)

    def test_real_reference_is_preserved_and_wrong_types_are_rejected(self):
        self.assertEqual(ClaimCheck(claim="Claim", result="MATCHES", ref="C1").ref, "C1")
        with self.assertRaises(ValidationError):
            ClaimCheck(claim="Claim", result="MATCHES", ref={"unexpected": "object"})


class SentimentContextPipelineTests(unittest.IsolatedAsyncioTestCase):
    async def test_complete_assessment_with_null_ref_passes_real_evidence_verification(self):
        payload = deepcopy(RESPONSE)
        payload["context_report"]["facts"]["claims"][0]["result"] = "MATCHES"
        generate = AsyncMock(return_value=BedrockResponse(json.dumps(payload)))
        with patch.object(engine, "_prompt", return_value="Synthetic post"), \
             patch.object(engine.ai_observability, "generate", generate):
            result = await engine._generate_with(object(), "test-model", {
                "title": "Test", "caption": "Drones can inspect buildings.", "guidelines": [], "context": [],
            }, None)
        self.assertEqual(result.summary, RESPONSE["summary"])
        self.assertEqual(result.context_report.facts.claims[0].result, "NO_FACT")
        generate.assert_awaited_once()
