import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch
from uuid import uuid4

from services.compliance.analytics import listening_providers as lp
from services.compliance.analytics import public_mood_service as pms


class XSentimentAndPublicMoodTests(unittest.IsolatedAsyncioTestCase):
    def test_comment_sentiment_classifies_positive_negative_and_neutral_with_vader_lexicon(self):
        pos = lp.classify_comment_sentiment("This product is amazing. Great service!")
        self.assertEqual(pos["sentiment"], "POSITIVE")
        self.assertGreaterEqual(pos["confidence"], 80.0)

        neg = lp.classify_comment_sentiment("Terrible customer service. I want a refund.")
        self.assertEqual(neg["sentiment"], "NEGATIVE")
        self.assertGreaterEqual(neg["confidence"], 70.0)
        self.assertIn("terrible", neg["crisis_flags"])

        neu = lp.classify_comment_sentiment("The company released a new product today.")
        self.assertEqual(neu["sentiment"], "NEUTRAL")
        self.assertGreaterEqual(neu["confidence"], 90.0)

    def test_publishing_decision_rules_post_wait_and_hold(self):
        # 1. POST: >= 70% positive, low negative, no severe crisis alerts
        rec, reason = lp.evaluate_publishing_decision(
            positive_pct=78.0,
            neutral_pct=15.0,
            negative_pct=7.0,
            volume=1200,
            volume_change_pct=38.0,
            is_spike=True,
            top_posts=[{"text": "Amazing precision agriculture drone demo!"}],
        )
        self.assertEqual(rec, "POST")
        self.assertIn("78% positive", reason)

        # 2. HOLD: Severe VADER intensity on attack drone raids
        rec_crisis, reason_crisis = lp.evaluate_publishing_decision(
            positive_pct=30.0,
            neutral_pct=30.0,
            negative_pct=40.0,
            volume=5000,
            volume_change_pct=90.0,
            is_spike=True,
            top_posts=[
                {"text": "Breaking: Multiple border towns raided by attack drones overnight, causing civilian casualties and widespread outrage."}
            ],
        )
        self.assertEqual(rec_crisis, "HOLD")
        self.assertIn("attack", reason_crisis)

        # 3. WAIT: Mixed sentiment without severe crisis intensity
        rec_wait, _ = lp.evaluate_publishing_decision(
            positive_pct=42.0,
            neutral_pct=35.0,
            negative_pct=23.0,
            volume=800,
            volume_change_pct=10.0,
            is_spike=False,
            top_posts=[{"text": "Curious how regulation lands this year."}],
        )
        self.assertEqual(rec_wait, "WAIT")

    async def test_x_json_provider_reads_and_scores_comments(self):
        with tempfile.TemporaryDirectory() as tmp:
            json_path = Path(tmp) / "x_feed.json"
            payload = {
                "topics": [
                    {
                        "external_id": "x-ag",
                        "name": "Agricultural drones",
                        "description": "Precision farming drones",
                        "keywords": ["agricultural drone", "crop spraying"],
                        "volume": 2400,
                        "volume_change_pct": 40.0,
                        "comments": [
                            {"author": "@f1", "text": "Amazing agricultural drone! Great yield and best service."},
                            {"author": "@f2", "text": "Love how fast this farm drone sprays crops."},
                            {"author": "@f3", "text": "Impressed by the autonomous field mapping demo."},
                            {"author": "@f4", "text": "New sensor spec sheet released today."},
                        ],
                    }
                ]
            }
            json_path.write_text(json.dumps(payload), encoding="utf-8")
            provider = lp.XJsonListeningProvider(json_path=json_path)

            topics = await provider.list_topics()
            self.assertEqual(len(topics), 1)
            self.assertEqual(topics[0]["external_id"], "x-ag")

            snap = await provider.snapshot("x-ag")
            self.assertEqual(snap["positive"], 75.0)
            self.assertEqual(snap["neutral"], 25.0)
            self.assertEqual(snap["negative"], 0.0)
            self.assertEqual(len(snap["top_posts"]), 4)

    async def test_load_submission_includes_script_and_video_transcript_for_attack_drone_screening(self):
        sub_id = uuid4()
        topic_id = uuid4()
        snap_id = uuid4()

        fake_submission_row = {
            "id": sub_id,
            "title": "Next-Gen Tactical Capabilities",
            "caption_text": "See our newest autonomous flight platform in action.",
            "brand_code": "ZENADRONE",
            "audience": "defense",
            "platforms": ["X", "LINKEDIN"],
            "current_version": 1,
            "brand_name": "ZenaDrone",
            "transcript": json.dumps([
                {"text": "In this video we showcase how our attack drones lock onto targets."}
            ]),
            "script_version_body": "SCENE 1: Narrator explains how military attack drones are deployed in raids.",
            "script_file_text": None,
        }

        with patch.object(pms, "fetch_one", new=AsyncMock(return_value=fake_submission_row)):
            loaded = await pms._load(sub_id)
            self.assertIn("attack drones lock onto targets", loaded["transcript_text"])
            self.assertIn("military attack drones are deployed", loaded["script_text"])
            self.assertIn("attack drones", loaded["text"])

        topics = [
            {
                "id": topic_id,
                "external_id": "x-counter-uas",
                "name": "Attack Drones & Counter-UAS",
                "keywords": ["attack drone", "attack drones", "drone raid"],
            }
        ]
        matches = pms._match(loaded["text"], topics, meaning=None)
        self.assertEqual(len(matches), 1)
        self.assertIn("attack drones", matches[0][1])

        snap_model = pms._snapshot_model({
            "id": snap_id,
            "window_days": 7,
            "positive_pct": 10.0,
            "neutral_pct": 20.0,
            "negative_pct": 70.0,
            "volume": 14800,
            "volume_change_pct": 115.0,
            "is_spike": True,
            "daily_volume": [1000] * 7,
            "top_posts": [
                {
                    "network": "X",
                    "author": "@news",
                    "text": "Breaking: Multiple border towns raided by attack drones overnight, causing civilian casualties and widespread outrage.",
                    "sentiment": "NEGATIVE",
                    "confidence": 96.0,
                    "engagements": 8900,
                }
            ],
            "fetched_at": pms.datetime.now(pms.timezone.utc),
        })
        self.assertEqual(snap_model.recommendation, "HOLD")

        matched_topic = pms.MatchedTopic(
            topic_id=topic_id,
            name="Attack Drones & Counter-UAS",
            matched_terms=["attack drones"],
            snapshot=snap_model,
        )
        overall_rec, overall_reason = pms._overall_post_recommendation([matched_topic])
        self.assertEqual(overall_rec, "HOLD")
        self.assertIn("Attack Drones & Counter-UAS", overall_reason)


if __name__ == "__main__":
    unittest.main()
