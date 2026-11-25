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
        self.assertIn("Source Link Fact-Check", st.degraded_features[0].affected_features)

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


class CancelledHealthTests(unittest.TestCase):
    def test_cancelled_request_does_not_turn_a_healthy_provider_into_an_outage(self):
        now = datetime.now(timezone.utc)
        rows = [
            {'status':'CANCELLED','kind':None,'feature':'VISUAL_COMPLIANCE','started_at':now-timedelta(minutes=4)},
            {'status':'OK','kind':None,'feature':'VISUAL_COMPLIANCE','started_at':now-timedelta(minutes=5)},
        ]
        health = ai_health.summarize(rows, now, key_set=True)
        self.assertTrue(health.available)
        self.assertEqual(health.degraded_features, [])

    def test_only_cancelled_calls_do_not_claim_a_provider_outage(self):
        now = datetime.now(timezone.utc)
        self.assertTrue(ai_health.summarize([{'status':'CANCELLED','feature':'SOURCE_CHECK','started_at':now}],now).available)

    def test_real_provider_errors_are_not_hidden_by_a_cancellation(self):
        now = datetime.now(timezone.utc)
        rows = [{'status':'CANCELLED','feature':'SOURCE_CHECK','started_at':now},
                {'status':'ERROR','kind':'KEY','feature':'SOURCE_CHECK','started_at':now-timedelta(seconds=10)}]
        self.assertFalse(ai_health.summarize(rows,now).available)


class ScopedFailureTests(unittest.TestCase):
    def row(self, now, feature, status="ERROR", kind="OTHER", seconds=0):
        return dict(status=status, kind=kind, feature=feature, started_at=now-timedelta(seconds=seconds))

    def test_production_sentiment_failures_do_not_disable_other_features(self):
        now = datetime.now(timezone.utc)
        rows = [self.row(now, "SENTIMENT"), self.row(now, "SENTIMENT", seconds=55),
                self.row(now, "COMPLIANCE_CHECK", "OK", None, 540)]
        result = ai_health.summarize(rows, now)
        self.assertTrue(result.available)
        self.assertEqual([i.feature for i in result.degraded_features], ["SENTIMENT"])

    def test_response_limits_across_features_are_not_provider_outages(self):
        now = datetime.now(timezone.utc)
        rows = [self.row(now, "SENTIMENT", kind="OUTPUT_LIMIT"),
                self.row(now, "SOURCE_CHECK", kind="RESPONSE_INVALID")]
        result = ai_health.summarize(rows, now)
        self.assertTrue(result.available)
        self.assertEqual(len(result.degraded_features), 2)

    def test_correlated_provider_failures_still_raise_global_banner(self):
        now = datetime.now(timezone.utc)
        result = ai_health.summarize([self.row(now, "SENTIMENT", kind="BUSY"),
                                     self.row(now, "COMPLIANCE_CHECK", kind="BUSY", seconds=3)], now)
        self.assertFalse(result.available)
        self.assertEqual(result.reason, "BUSY")

    def test_quota_on_one_model_feature_is_not_global(self):
        now = datetime.now(timezone.utc)
        result = ai_health.summarize([self.row(now, "SENTIMENT", kind="QUOTA")], now)
        self.assertTrue(result.available)
        self.assertEqual(result.degraded_features[0].status, "OVERFLOW")

    def test_configured_key_failure_is_still_global(self):
        now = datetime.now(timezone.utc)
        self.assertFalse(ai_health.summarize([], now, key_set=False).available)
