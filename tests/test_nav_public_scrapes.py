"""Nav public mood scrapings: Financial ZenaTech sentiment + Today's Posts public mood."""

import asyncio
import os
import unittest
from datetime import datetime, timedelta, timezone
from email.utils import format_datetime
from unittest.mock import AsyncMock, patch
from uuid import uuid4

os.environ.setdefault("JWT_SECRET", "test-only-secret-for-unit-tests-0123456789")

from fastapi import BackgroundTasks

from models.compliance.public_mood_model import ListeningPost, ListeningTopic, TopicSnapshot
from services.compliance.analytics import nav_public_mood_service as npm
from services.compliance.analytics import post_queries as pq
from services.compliance.analytics import zenatech_financial_scrape as zfs
from tools.compliance.analytics import listening_router as lr


def _topic(ext: str, name: str, text: str = "ZenaTech news") -> ListeningTopic:
    now = datetime.now(timezone.utc)
    return ListeningTopic(
        id=uuid4(),
        external_id=ext,
        name=name,
        description=f"Desc for {name}",
        keywords=["ZenaTech"],
        synced_at=now,
        snapshot=TopicSnapshot(
            id=uuid4(),
            window_days=14,
            positive_pct=70.0,
            neutral_pct=20.0,
            negative_pct=10.0,
            net_sentiment=60.0,
            volume=12,
            fetched_at=now,
            top_posts=[ListeningPost(network="Financial News", author="@YahooFinance", text=text, sentiment="POSITIVE")],
        ),
    )


class FinancialZenaTechScrapeTests(unittest.TestCase):
    def test_zenatech_anchor_filtering_keeps_company_and_drops_generic_ticker_noise(self):
        self.assertTrue(zfs.is_zenatech_relevant("ZenaTech (ZENA) Stock Gains After Drone Contract - Benzinga"))
        self.assertTrue(zfs.is_zenatech_relevant("$ZENA rallies on NASDAQ volume"))
        self.assertFalse(zfs.is_zenatech_relevant("symbol__ Stock Quote Price and Forecast - CNN"))
        self.assertFalse(zfs.is_zenatech_relevant("S&P 500 market rally continues - Reuters"))

    def test_parse_rss_keeps_recent_financial_items_and_drops_old_and_noise(self):
        fresh = format_datetime(datetime.now(timezone.utc) - timedelta(days=1))
        stale = format_datetime(datetime.now(timezone.utc) - timedelta(days=40))
        xml = f"""<rss><channel>
          <item><title>ZenaTech Secures Drone Contract</title><link>https://f.example/1</link><pubDate>{fresh}</pubDate></item>
          <item><title>symbol__ Stock Quote - CNN</title><link>https://f.example/2</link><pubDate>{fresh}</pubDate></item>
          <item><title>Old ZenaTech Article</title><link>https://f.example/3</link><pubDate>{stale}</pubDate></item>
        </channel></rss>"""
        items = zfs.parse_rss_items(xml, limit=10, max_age_days=14, default_network="Yahoo Finance")
        self.assertEqual([i["text"] for i in items], ["ZenaTech Secures Drone Contract"])
        self.assertEqual(items[0]["url"], "https://f.example/1")
        self.assertEqual(items[0]["network"], "Yahoo Finance")

    def test_collect_financial_topics_runs_rss_and_filters_unrelated_news(self):
        sources, pool = [], []

        async def fake_rss(url, net, auth):
            return ([{"text": "ZenaTech (ZENA) Q3 earnings preview", "url": "https://y/1", "network": net, "author": auth}], "SUCCESS")

        async def fake_news(q):
            return ([{"text": "ZenaTech expands DaaS footprint", "url": "https://g/1"},
                     {"text": "Unrelated macro headline", "url": "https://g/2"}], "SUCCESS")

        topics = asyncio.run(zfs.collect_financial_topics(fake_rss, fake_news, sources, lambda: False, pool))
        self.assertEqual([t["external_id"] for t in topics], ["financial-zenatech-stock", "financial-zenatech-buzz"])
        self.assertTrue(all(t["scrape_track"] == zfs.FINANCIAL_TRACK for t in topics))
        self.assertTrue(any(s["kind"] == "financial:rss" and s["items"] == 1 for s in sources))
        self.assertTrue(any(s["kind"] == "financial:news" and s.get("dropped_unrelated") == 1 for s in sources))
        self.assertFalse(any(p["item"]["text"] == "Unrelated macro headline" for p in pool))

    def test_merge_scoped_feed_preserves_the_other_track(self):
        existing = {
            "topics": [
                {"external_id": "financial-zenatech-stock", "scrape_track": zfs.FINANCIAL_TRACK, "comments": [1]},
                {"external_id": "post-abc", "scrape_track": pq.TODAY_TRACK, "comments": [2]},
            ],
            "collection": {"sources": [
                {"kind": "financial:rss", "name": "Yahoo", "status": "SUCCESS", "items": 1},
                {"kind": "post:news", "name": "Draft Q", "status": "SUCCESS", "items": 1},
            ]},
        }
        topics_fin, sources_fin = zfs.merge_scoped_feed(
            "financial", existing,
            [{"external_id": "financial-zenatech-stock", "scrape_track": zfs.FINANCIAL_TRACK, "comments": [9]}],
            [{"kind": "financial:rss", "name": "Yahoo", "status": "SUCCESS", "items": 9}],
        )
        self.assertEqual([t["external_id"] for t in topics_fin], ["financial-zenatech-stock", "post-abc"])
        self.assertEqual([s["kind"] for s in sources_fin], ["financial:rss", "post:news"])

        topics_today, _ = zfs.merge_scoped_feed(
            "today", existing,
            [{"external_id": "post-xyz", "scrape_track": pq.TODAY_TRACK, "comments": [3]}],
            [{"kind": "post:news", "name": "Today Q", "status": "SUCCESS", "items": 1}],
        )
        self.assertEqual([t["external_id"] for t in topics_today], ["financial-zenatech-stock", "post-xyz"])


class TodayPostSelectionTests(unittest.TestCase):
    def test_scheduled_today_and_fixture_filtering(self):
        start, end = pq.today_window(datetime(2026, 11, 18, 17, 0, tzinfo=timezone.utc))
        scheduled = {"status": "APPROVED", "publish_at": start + timedelta(hours=3), "caption_text": "Image compliance test fixture. NOT FOR PUBLICATION."}
        activity = {"status": "IN_REVIEW", "publish_at": None, "updated_at": start + timedelta(hours=2), "caption_text": "ZenaDrone field demo"}
        fixture = {"status": "DRAFT", "publish_at": None, "updated_at": start + timedelta(hours=2), "caption_text": "Image compliance test fixture. NOT FOR PUBLICATION."}
        old = {"status": "DRAFT", "publish_at": start - timedelta(days=2), "updated_at": start - timedelta(days=2), "caption_text": "Old post"}
        self.assertEqual(pq.today_reason_for_row(scheduled, start, end), "scheduled_today")
        self.assertEqual(pq.today_reason_for_row(activity, start, end), "today_activity")
        self.assertIsNone(pq.today_reason_for_row(fixture, start, end))
        self.assertIsNone(pq.today_reason_for_row(old, start, end))

    def test_topic_for_draft_marks_today_track_only_when_is_today(self):
        today_t = pq.topic_for_draft({"id": "1", "title": "P", "keywords": ["k"], "is_today": True, "today_reason": "scheduled_today", "submission_status": "APPROVED"})
        old_t = pq.topic_for_draft({"id": "2", "title": "Q", "keywords": ["k"]})
        self.assertEqual(today_t["scrape_track"], pq.TODAY_TRACK)
        self.assertEqual(today_t["post_meta"]["today_reason"], "scheduled_today")
        self.assertNotIn("scrape_track", old_t)


class NavPublicMoodAndRouterTests(unittest.TestCase):
    def test_nav_splits_financial_zenatech_and_todays_posts(self):
        pid = uuid4()
        other = uuid4()
        feed = {
            "collected_at": datetime.now(timezone.utc).isoformat(),
            "collection": {"sources": [
                {"kind": "financial:rss", "name": "Yahoo Finance", "status": "SUCCESS", "items": 5},
                {"kind": "post:news", "name": "ZenaDrone Q", "status": "SUCCESS", "items": 3},
            ]},
            "topics": [
                {"external_id": "financial-zenatech-stock", "scrape_track": zfs.FINANCIAL_TRACK,
                 "comments": [{"text": "ZENA jumps", "url": "https://y/zena"}]},
                {"external_id": f"post-{pid}", "scrape_track": pq.TODAY_TRACK,
                 "comments": [{"text": "ZenaDrone today story", "url": "https://n/today"}]},
                {"external_id": f"post-{other}", "comments": [{"text": "Old draft story"}]},
            ],
        }
        topics = [
            _topic("financial-zenatech-stock", "ZenaTech Stock", "ZENA jumps"),
            _topic(f"post-{pid}", "Launch Post", "ZenaDrone today story"),
            _topic(f"post-{other}", "Old Draft", "Old draft story"),
        ]
        today_rows = [{"id": str(pid), "title": "Launch Post", "status": "APPROVED", "publish_at": datetime.now(timezone.utc), "today_reason": "scheduled_today", "has_searches": True}]

        with patch.object(npm, "load_feed", AsyncMock(return_value=feed)), \
                patch.object(npm.post_queries, "list_today_post_candidates", AsyncMock(return_value=today_rows)), \
                patch("services.compliance.analytics.public_mood_service.list_topics", AsyncMock(return_value=topics)):
            res = asyncio.run(npm.get_nav_public_mood())

        self.assertEqual([t.external_id for t in res.financial_zenatech.trends], ["financial-zenatech-stock"])
        self.assertEqual(res.financial_zenatech.trends[0].top_posts[0].url, "https://y/zena")
        self.assertEqual([t.external_id for t in res.today_posts.trends], [f"post-{pid}"])
        self.assertEqual(res.today_posts.trends[0].post_id, str(pid))
        self.assertEqual(res.today_posts.trends[0].post_title, "Launch Post")
        self.assertEqual(len(res.today_posts.posts), 1)

    def test_financial_scope_scrape_skips_draft_queries(self):
        lr._scrape.update(state="idle", scope=None, started_at=None, finished_at=None, error=None, collection=None)
        draft_mock = AsyncMock(return_value=[])
        with patch.object(lr.post_queries, "active_draft_searches", draft_mock), \
                patch.object(lr, "_run_scraper_process", return_value=unittest.mock.MagicMock(returncode=0, stdout="", stderr="")) as proc, \
                patch.object(lr, "reset_provider_cache"), \
                patch.object(lr.svc, "record_conversations", AsyncMock()), \
                patch.object(lr.svc, "list_topics", AsyncMock()), \
                patch.object(lr.svc, "list_trend_opportunities", AsyncMock()), \
                patch.object(lr.svc, "index_conversations", AsyncMock()), \
                patch.object(lr, "_collection_summary", AsyncMock(return_value={"status": "SUCCESS"})):
            asyncio.run(lr._run_scrape_and_refresh("financial"))
        draft_mock.assert_not_awaited()
        proc.assert_called_once_with(None, "financial")
        tasks = BackgroundTasks()
        res = asyncio.run(lr.trigger_live_scrape(tasks, uuid4(), scope="today"))
        self.assertEqual((res["status"], res["scope"]), ("started", "today"))

    def test_financial_topics_do_not_leak_into_individual_submission_mood(self):
        from services.compliance.analytics import public_mood_service as pms

        sub_id = uuid4()
        self.assertFalse(pms._may_show_on({"external_id": "financial-zenatech-stock"}, sub_id))
        self.assertFalse(pms._may_show_on({"external_id": "financial-zenatech-buzz"}, sub_id))
        self.assertTrue(pms._may_show_on({"external_id": f"post-{sub_id}"}, sub_id))
        self.assertTrue(pms._may_show_on({"external_id": "watch-company"}, sub_id))

    def test_financial_pool_and_post_pool_do_not_steal_stories(self):
        from scripts import scrape_x_playwright as sx

        story = "ZenaTech Announces Q3 Revenue Growth - Yahoo Finance"
        fin = sx.assign_unique([{"topic": "financial-zenatech-stock", "score": 1005, "item": {"text": story}}], ["financial-zenatech-stock"])
        post = sx.assign_unique([{"topic": "post-1", "score": 1005, "item": {"text": story}}], ["post-1"])
        self.assertEqual(len(fin["financial-zenatech-stock"]), 1)
        self.assertEqual(len(post["post-1"]), 1)

    def test_today_posts_do_not_steal_stories_from_each_other(self):
        from scripts import scrape_x_playwright as sx

        story = "Image Finding Milestone - Reuters"
        topics = [
            {"external_id": "post-aaa"},
            {"external_id": "post-aaa-context-1"},
            {"external_id": "post-bbb-context-1"},
        ]
        pool = [
            {"topic": "post-aaa", "score": 1005, "item": {"text": story}},
            {"topic": "post-aaa-context-1", "score": 1002, "item": {"text": story}},
            {"topic": "post-bbb-context-1", "score": 1002, "item": {"text": story}},
        ]
        homes = pq.assign_unique_per_post(pool, topics, sx.assign_unique)
        # Deduped within post-aaa, but post-bbb keeps its own copy rather than getting starved.
        self.assertEqual(len(homes["post-aaa"]), 1)
        self.assertEqual(len(homes["post-aaa-context-1"]), 0)
        self.assertEqual(len(homes["post-bbb-context-1"]), 1)


class DailyScrapeScheduleTests(unittest.TestCase):
    def test_next_scheduled_run_is_830_today_or_tomorrow_in_app_timezone(self):
        from zoneinfo import ZoneInfo
        from services.compliance.analytics import daily_scrape_schedule as ds

        ny = ZoneInfo("America/New_York")
        morning = datetime(2026, 11, 18, 8, 15, tzinfo=ny).astimezone(timezone.utc)
        after = datetime(2026, 11, 18, 8, 35, tzinfo=ny).astimezone(timezone.utc)
        nxt_morning = ds.next_scheduled_run(morning, hour=8, minute=30, tz_name="America/New_York").astimezone(ny)
        nxt_after = ds.next_scheduled_run(after, hour=8, minute=30, tz_name="America/New_York").astimezone(ny)
        self.assertEqual((nxt_morning.day, nxt_morning.hour, nxt_morning.minute), (18, 8, 30))
        self.assertEqual((nxt_after.day, nxt_after.hour, nxt_after.minute), (19, 8, 30))

    def test_catch_up_missed_run_respects_grace_window_and_last_collection(self):
        from zoneinfo import ZoneInfo
        from services.compliance.analytics import daily_scrape_schedule as ds

        ny = ZoneInfo("America/New_York")
        at_845 = datetime(2026, 11, 18, 8, 45, tzinfo=ny).astimezone(timezone.utc)
        at_1345 = datetime(2026, 11, 18, 13, 45, tzinfo=ny).astimezone(timezone.utc)
        yesterday = datetime(2026, 11, 17, 15, 0, tzinfo=timezone.utc)
        ran_at_831 = datetime(2026, 11, 18, 8, 31, tzinfo=ny).astimezone(timezone.utc)

        self.assertTrue(ds.should_catch_up_missed_run(at_845, yesterday, tz_name="America/New_York", grace_minutes=60))
        self.assertFalse(ds.should_catch_up_missed_run(at_845, ran_at_831, tz_name="America/New_York", grace_minutes=60))
        self.assertFalse(ds.should_catch_up_missed_run(at_1345, yesterday, tz_name="America/New_York", grace_minutes=60))

    def test_daily_scrape_loop_fires_at_830_once_and_advances(self):
        from zoneinfo import ZoneInfo
        from services.compliance.analytics import daily_scrape_schedule as ds

        ny = ZoneInfo("America/New_York")
        clock = [datetime(2026, 11, 18, 8, 29, tzinfo=ny).astimezone(timezone.utc)]
        runs = []

        async def fake_sleep(secs: float):
            clock[0] = clock[0] + timedelta(seconds=secs)

        async def run_scrape():
            runs.append(clock[0])

        asyncio.run(ds.daily_scrape_loop(
            run_scrape,
            lambda: False,
            AsyncMock(return_value=None),
            enabled=True,
            tz_name="America/New_York",
            now_fn=lambda: clock[0],
            sleep_fn=fake_sleep,
            max_iterations=2,
        ))
        self.assertEqual(len(runs), 1)
        self.assertEqual((runs[0].astimezone(ny).hour, runs[0].astimezone(ny).minute), (8, 30))


class FinancialSentimentRoutingTests(unittest.TestCase):
    def test_financial_topics_use_finbert_and_non_financial_use_default_model(self):
        from services.compliance.analytics import listening_providers as lp

        fin_res = {"sentiment": "POSITIVE", "confidence": 90.2, "compound": 0.89, "crisis_flags": [], "model": lp.FINANCIAL_MODEL_NAME}
        gen_res = {"sentiment": "NEUTRAL", "confidence": 82.0, "compound": 0.0, "crisis_flags": [], "model": lp.LOCAL_MODEL_NAME}
        with patch.object(lp, "classify_financial_sentiment", return_value=fin_res) as fin_mock, \
                patch.object(lp, "classify_comment_sentiment", return_value=gen_res) as gen_mock:
            fin_out = lp.analyze_raw_x_topic({
                "external_id": "financial-zenatech-stock",
                "category": "financial",
                "comments": [{"text": "ZenaTech wins solar contract"}],
            })
            post_out = lp.analyze_raw_x_topic({
                "external_id": "post-123",
                "comments": [{"text": "Great field demo today"}],
            })
        fin_mock.assert_called_once_with("ZenaTech wins solar contract")
        gen_mock.assert_called_once_with("Great field demo today")
        self.assertEqual(fin_out["positive"], 100.0)
        self.assertEqual(post_out["neutral"], 100.0)
        with patch.object(lp, "classify_financial_sentiment", return_value=fin_res) as fin_direct, \
                patch.object(lp, "classify_comment_sentiment", return_value=gen_res) as gen_direct:
            self.assertEqual(lp.classify_topic_sentiment("ZENA guidance raised", {"external_id": "financial-zenatech-stock"})["model"], lp.FINANCIAL_MODEL_NAME)
            self.assertEqual(lp.classify_topic_sentiment("Field demo photo", {"external_id": "post-1"})["model"], lp.LOCAL_MODEL_NAME)
        fin_direct.assert_called_once_with("ZENA guidance raised")
        gen_direct.assert_called_once_with("Field demo photo")

    def test_financial_sentiment_model_scoring_and_vader_fallback(self):
        from services.compliance.analytics import item_sentiment as is_s

        with patch.object(is_s, "_financial_model", False):
            vader_res = is_s.classify_financial_sentiment("Terrible quarterly loss and weak guidance.")
        self.assertEqual(vader_res["sentiment"], "NEGATIVE")
        self.assertNotIn("model", vader_res)

        probs = {"POSITIVE": 0.91, "NEUTRAL": 0.06, "NEGATIVE": 0.03}
        with patch.object(is_s, "_financial_probs", return_value=probs):
            fin_res = is_s.classify_financial_sentiment("ZenaTech wins US$1M solar contract")
        self.assertEqual(fin_res["sentiment"], "POSITIVE")
        self.assertEqual(fin_res["confidence"], 91.0)
        self.assertEqual(fin_res["model"], is_s.FINANCIAL_MODEL_NAME)

    def test_real_finbert_scores_zenatech_contract_win_positive_and_miss_negative(self):
        from services.compliance.analytics import item_sentiment as is_s

        is_s._financial_model = None
        if not is_s.load_financial_model():
            self.skipTest("FinBERT sentiment model isn't available")
        win = is_s.classify_financial_sentiment(
            "ZenaTech's Drone as a Service Enters Solar Surveying Market with U.S. School Contract Wins, "
            "Targeting $600,000 in Revenue from First Solar Client"
        )
        miss = is_s.classify_financial_sentiment("ZenaTech Q3 Revenue Misses Estimates and Stock Drops 18%")
        self.assertEqual(win["sentiment"], "POSITIVE")
        self.assertEqual(miss["sentiment"], "NEGATIVE")
        self.assertEqual(win["model"], is_s.FINANCIAL_MODEL_NAME)


if __name__ == "__main__":
    unittest.main()
