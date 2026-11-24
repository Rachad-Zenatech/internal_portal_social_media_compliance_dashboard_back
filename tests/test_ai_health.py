"""Tests for per-feature AI outage & overflow detection in ai_health."""
import unittest
from datetime import datetime, timedelta, timezone

from services.ai.observability import ai_health


class AiHealthFeatureTests(unittest.TestCase):
    def test_single_feature_outage_is_surfaced_even_when_other_features_pass(self):
        now = datetime.now(timezone.utc)
        rows = [
            {"status": "OK", "kind": None, "feature": "COMPLIANCE_CHECK", "started_at": now},
            {"status": "ERROR", "kind": "OTHER", "feature": "SOURCE_CHECK", "started_at": now - timedelta(seconds=10)},
            {"status": "ERROR", "kind": "OTHER", "feature": "SOURCE_CHECK", "started_at": now - timedelta(seconds=20)},
        ]
        st = ai_health.summarize(rows, now, key_set=True)
        self.assertTrue(st.available)
        self.assertEqual(len(st.degraded_features), 1)
        self.assertEqual(st.degraded_features[0].feature, "SOURCE_CHECK")
        self.assertEqual(st.degraded_features[0].status, "OUT")

    def test_quota_or_busy_surfaces_as_overflow(self):
        now = datetime.now(timezone.utc)
        rows = [
            {"status": "ERROR", "kind": "QUOTA", "feature": "TRANSCRIPTION", "started_at": now - timedelta(seconds=5)},
            {"status": "OK", "kind": None, "feature": "CHAT", "started_at": now - timedelta(seconds=10)},
        ]
        st = ai_health.summarize(rows, now, key_set=True)
        self.assertEqual(len(st.degraded_features), 1)
        self.assertEqual(st.degraded_features[0].feature, "TRANSCRIPTION")
        self.assertEqual(st.degraded_features[0].status, "OVERFLOW")

    def test_recovery_clears_feature_issue(self):
        now = datetime.now(timezone.utc)
        rows = [
            {"status": "OK", "kind": None, "feature": "SOURCE_CHECK", "started_at": now},
            {"status": "ERROR", "kind": "QUOTA", "feature": "SOURCE_CHECK", "started_at": now - timedelta(seconds=30)},
        ]
        st = ai_health.summarize(rows, now, key_set=True)
        self.assertTrue(st.available)
        self.assertEqual(st.degraded_features, [])
