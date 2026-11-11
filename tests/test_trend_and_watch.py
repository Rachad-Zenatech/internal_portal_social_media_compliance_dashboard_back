"""A real trend from what has been collected, and searches about the company itself."""

import asyncio
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch

from scripts import scrape_x_playwright as sx
from services.compliance.analytics import conversation_evidence as ce
from services.compliance.analytics import listening_providers as lp
from services.compliance.analytics import listening_sources as ls
from services.compliance.analytics import public_mood_service as pms

TOPIC = {"id": "t-1", "external_id": "trend-delivery", "name": "Drone Delivery"}


class VolumeChangeTests(unittest.TestCase):
    def test_a_steady_volume_reads_as_steady_not_as_growth(self):
        # The old rule compared the first 3 days with the last 4, so flat volume read as +33%: nearly a spike.
        self.assertEqual(lp.volume_change([5] * 7), 0.0)
        self.assertEqual(lp.volume_change([10] * 7), 0.0)

    def test_growth_and_decline_compare_the_last_three_complete_days_with_the_three_before(self):
        self.assertEqual(lp.volume_change([4, 4, 4, 10, 10, 10, 2]), 150.0)
        self.assertEqual(lp.volume_change([10, 10, 10, 4, 4, 4, 9]), -60.0)

    def test_today_is_left_out_because_it_is_still_filling_up(self):
        self.assertEqual(lp.volume_change([5, 5, 5, 5, 5, 5, 0]), lp.volume_change([5, 5, 5, 5, 5, 5, 400]))

    def test_with_too_little_earlier_to_compare_the_change_is_unknown_not_a_big_number(self):
        self.assertIsNone(lp.volume_change([0, 1, 1, 3, 3, 3, 3]))  # two stories before: "+350%" would be noise
        self.assertIsNone(lp.volume_change([3, 3, 3, 9, 9, 9, 0]))  # nine before: still too few to call a change
        self.assertEqual(lp.volume_change([3, 3, 4, 9, 9, 9, 0]), 170.0)  # ten before: enough
        self.assertIsNone(lp.volume_change([5, 5, 5]))              # fewer than a week of days

    def test_analyze_uses_the_same_rule_and_keeps_a_sources_own_figure(self):
        out = lp.analyze_raw_x_topic({"comments": [{"text": "a headline"}], "daily_volume": [5] * 7})
        self.assertEqual(out["volume_change_pct"], 0.0)
        out = lp.analyze_raw_x_topic({"comments": [{"text": "a headline"}], "daily_volume": [5] * 7, "volume_change_pct": 80})
        self.assertEqual(out["volume_change_pct"], 80.0)


class TopicMoodTests(unittest.TestCase):
    def test_the_same_story_from_several_outlets_counts_once(self):
        out = lp.analyze_raw_x_topic({"comments": [
            {"text": "Great progress, I love this drone news - Outlet A"},
            {"text": "Great progress, I love this drone news - Outlet B"},
            {"text": "Great progress, I love this drone news"},
            {"text": "Terrible outage, customers furious and angry"},
        ]})
        self.assertEqual(out["volume"], 2)
        self.assertEqual((out["positive"], out["negative"]), (50.0, 50.0))

    def test_items_already_scored_are_not_run_through_the_model_again(self):
        with patch.object(lp, "classify_comment_sentiment", side_effect=AssertionError("re-scored")):
            out = lp.analyze_raw_x_topic({"comments": [{"text": "x story", "sentiment": "POSITIVE", "confidence": 91.0}]})
        self.assertEqual(out["positive"], 100.0)
        self.assertEqual(out["top_posts"][0]["confidence"], 91.0)

    def test_an_item_without_a_score_is_still_scored(self):
        out = lp.analyze_raw_x_topic({"comments": [{"text": "Terrible customer service. I want a refund."}]})
        self.assertEqual(out["negative"], 100.0)


class DailyCountsTests(unittest.TestCase):
    def _counts(self, rows, days=7):
        with patch.object(ce, "fetch_all", AsyncMock(return_value=rows)):
            return asyncio.run(ce.topic_daily_counts("trend-delivery", days))

    def test_days_with_nothing_are_zero_and_the_list_runs_oldest_to_today(self):
        today = datetime.now(timezone.utc).date()
        rows = [{"day": today, "n": 4}, {"day": today - timedelta(days=2), "n": 7}, {"day": today - timedelta(days=6), "n": 1}]
        self.assertEqual(self._counts(rows), [1, 0, 0, 0, 7, 0, 4])

    def test_nothing_collected_is_all_zeros(self):
        self.assertEqual(self._counts([]), [0] * 7)

    def test_a_shorter_window(self):
        today = datetime.now(timezone.utc).date()
        self.assertEqual(self._counts([{"day": today, "n": 2}], days=3), [0, 0, 2])

    def test_a_story_is_counted_once_however_many_outlets_or_scrapes_carried_it(self):
        sql = {}

        async def fetch_all(query, *args):
            sql["q"] = query
            return []
        with patch.object(ce, "fetch_all", fetch_all):
            asyncio.run(ce.topic_daily_counts("x", 7))
        self.assertIn("DISTINCT ON (story_key)", sql["q"])  # across every scrape, not just the latest


class WithTrendTests(unittest.TestCase):
    def _run(self, snapshot, daily, provider="x_json"):
        fake = type("P", (), {"name": provider})()
        with patch.object(pms, "get_provider", return_value=fake), \
                patch.object(pms.conversation_evidence, "topic_daily_counts", AsyncMock(return_value=daily)):
            return asyncio.run(pms._with_trend(snapshot, TOPIC))

    def test_real_counts_and_a_real_change_replace_unknown(self):
        out = self._run({"volume": 15, "volume_change_pct": None, "daily_volume": []}, [4, 4, 4, 10, 10, 10, 2])
        self.assertEqual(out["daily_volume"], [4, 4, 4, 10, 10, 10, 2])
        self.assertEqual(out["volume_change_pct"], 150.0)
        self.assertEqual(out["volume"], 15)  # the sample behind the percentages is untouched

    def test_too_little_history_gives_the_counts_but_no_change(self):
        out = self._run({"volume": 15, "volume_change_pct": None, "daily_volume": []}, [0, 0, 1, 1, 1, 2, 1])
        self.assertEqual(out["daily_volume"], [0, 0, 1, 1, 1, 2, 1])
        self.assertIsNone(out["volume_change_pct"])

    def test_nothing_collected_leaves_the_snapshot_as_it_was(self):
        snap = {"volume": 0, "volume_change_pct": None, "daily_volume": []}
        self.assertEqual(self._run(snap, [0] * 7), snap)

    def test_a_sources_own_figures_are_kept(self):
        snap = {"volume": 900, "volume_change_pct": 12.0, "daily_volume": [1, 2, 3, 4, 5, 6, 7]}
        out = self._run(snap, [9, 9, 9, 9, 9, 9, 9])
        self.assertEqual((out["volume_change_pct"], out["daily_volume"]), (12.0, [1, 2, 3, 4, 5, 6, 7]))

    def test_the_sample_source_is_left_alone(self):
        snap = {"volume": 40, "volume_change_pct": None, "daily_volume": []}
        self.assertEqual(self._run(snap, [5] * 7, provider="mock"), snap)

    def test_a_failure_reading_the_history_does_not_break_the_snapshot(self):
        fake = type("P", (), {"name": "x_json"})()
        snap = {"volume": 3, "volume_change_pct": None, "daily_volume": []}
        with patch.object(pms, "get_provider", return_value=fake), \
                patch.object(pms.conversation_evidence, "topic_daily_counts", AsyncMock(side_effect=RuntimeError("db"))):
            self.assertEqual(asyncio.run(pms._with_trend(snap, TOPIC)), snap)

    def test_a_spike_needs_a_real_jump_over_enough_earlier_stories(self):
        out = self._run({"volume": 15, "volume_change_pct": None, "daily_volume": []}, [4, 4, 4, 8, 8, 8, 2])
        self.assertGreaterEqual(out["volume_change_pct"], pms.SPIKE_CHANGE_PCT)  # +100% over 12 earlier stories
        flat = self._run({"volume": 15, "volume_change_pct": None, "daily_volume": []}, [4] * 7)
        self.assertLess(flat["volume_change_pct"], pms.SPIKE_CHANGE_PCT)


class WatchPlanTests(unittest.TestCase):
    def _by_cat(self, plan):
        out = {}
        for q in ls.build_queries(plan):
            out.setdefault(q.category, []).append(q)
        return out

    def test_the_seed_plan_covers_names_hashtags_products_and_links_and_guesses_nothing_else(self):
        cats = self._by_cat(ls.load_plan())
        self.assertEqual({q.text for q in cats["company"]}, {"ZenaTech", "ZenaDrone"})
        self.assertIn("#ZooOffice", {q.text for q in cats["hashtags"]})
        self.assertEqual([q.text for q in cats["links"]], ["zenatech.com"])
        self.assertNotIn("events", cats)
        self.assertNotIn("competitors", cats)

    def test_a_hashtag_is_searched_as_its_word_and_a_domain_as_it_is(self):
        q = {q.text: q.news for q in ls.build_queries({"hashtags": ["#ZenaTech"], "links": ["zenatech.com"]})}
        self.assertEqual(q["#ZenaTech"], '"ZenaTech"')
        self.assertEqual(q["zenatech.com"], '"zenatech.com"')

    def test_an_empty_or_missing_plan_builds_nothing(self):
        self.assertEqual(ls.build_queries({}), [])
        self.assertEqual(ls.build_queries({"company": ["", "  "]}), [])

    def test_the_team_can_add_events_and_competitors_without_code(self):
        cats = self._by_cat({"events": ["Commercial UAV Expo"], "competitors": ["Acme Drones"]})
        self.assertEqual(cats["events"][0].news, '"Commercial UAV Expo"')
        self.assertEqual(cats["competitors"][0].category, "competitors")

    def test_a_broken_plan_file_means_no_searches_not_a_crash(self):
        import tempfile
        from pathlib import Path
        with tempfile.TemporaryDirectory() as tmp:
            bad = Path(tmp) / "q.json"
            bad.write_text("{not json")
            self.assertEqual(ls.load_plan(bad), {})
            self.assertEqual(ls.load_plan(Path(tmp) / "missing.json"), {})

    def test_each_category_becomes_a_topic_the_pipeline_already_understands(self):
        queries = ls.build_queries(ls.load_plan())
        topic = ls.topic_for("company", queries)
        self.assertEqual((topic["external_id"], topic["name"]), ("watch-company", "Brand names"))
        self.assertIn("[HIGH]", topic["description"])
        self.assertEqual(set(topic["keywords"]), {"ZenaTech", "ZenaDrone"})
        self.assertEqual(ls.topic_for("hashtags", queries)["keywords"][0], "ZenaTech")  # no '#': matched as a word


class OneHomePerStoryTests(unittest.TestCase):
    """A story is counted in one topic, not in every topic it shares a word with."""

    def _cand(self, topic, text, score):
        return {"topic": topic, "score": score, "item": {"text": text}}

    def test_a_story_that_fits_three_topics_goes_to_the_one_that_claims_it_most(self):
        text = "Drone News Roundup: DJI Inspire 3 footage and more!"
        pool = [self._cand("ag", text, 1), self._cand("delivery", text, 3), self._cand("survey", text, 2)]
        homes = sx.assign_unique(pool, ["ag", "delivery", "survey"])
        self.assertEqual([len(homes[t]) for t in ("ag", "delivery", "survey")], [0, 1, 0])

    def test_a_tie_goes_to_the_earlier_topic(self):
        homes = sx.assign_unique([self._cand("b", "Same post", 2), self._cand("a", "Same post", 2)], ["a", "b"])
        self.assertEqual((len(homes["a"]), len(homes["b"])), (1, 0))

    def test_a_search_about_the_company_beats_a_market_topic(self):
        pool = [self._cand("market", "ZenaTech wins contract", 5), self._cand("watch-company", "ZenaTech wins contract", sx.WATCH_SCORE)]
        homes = sx.assign_unique(pool, ["watch-company", "market"])
        self.assertEqual((len(homes["watch-company"]), len(homes["market"])), (1, 0))

    def test_the_same_story_from_two_outlets_is_one_story(self):
        pool = [self._cand("a", "Drone delivery expands - The Guardian", 2), self._cand("b", "Drone delivery expands - Yahoo News", 2)]
        self.assertEqual(sum(len(v) for v in sx.assign_unique(pool, ["a", "b"]).values()), 1)

    def test_topic_score_prefers_the_topic_whose_phrases_the_text_uses(self):
        ag = next(t for t in sx.TRENDING_MARKET_TOPICS if "ag-drones" in t["external_id"])
        delivery = next(t for t in sx.TRENDING_MARKET_TOPICS if "delivery" in t["external_id"])
        text = "Precision agriculture crop spraying drones help farmers"
        self.assertGreater(sx.topic_score(text, ag), sx.topic_score(text, delivery))


class WatchCollectionTests(unittest.TestCase):
    def test_an_unconfigured_category_is_reported_and_is_not_a_collection_problem(self):
        sources = []
        asyncio.run(sx._collect_watch(None, ls.build_queries({"company": []}), sources, lambda: False, []))
        self.assertTrue(all(s["status"] == "NOT_CONFIGURED" for s in sources))
        self.assertEqual(sx.overall_status([{"status": "SUCCESS"}, {"status": "NOT_CONFIGURED"}], 5, False), "SUCCESS")

    def test_company_searches_keep_the_companys_own_coverage_and_market_searches_do_not(self):
        seen = []

        async def news(page, query, limit=10, exclude_terms=None, drop_own_brand=True):
            seen.append((query, drop_own_brand))
            return [{"text": f"{query} headline", "author": "@x", "url": "https://n/1", "published_at": None}], "SUCCESS"
        sources, pool = [], []
        with patch.object(sx, "_scrape_live_trend_news_playwright", news):
            topics = asyncio.run(sx._collect_watch(None, ls.build_queries({"company": ["ZenaTech"]}), sources, lambda: False, pool))
        self.assertEqual(seen, [('"ZenaTech"', False)])
        self.assertEqual([t["external_id"] for t in topics], ["watch-company"])
        self.assertEqual((pool[0]["topic"], pool[0]["score"]), ("watch-company", sx.WATCH_SCORE))
        self.assertEqual((pool[0]["item"]["category"], pool[0]["item"]["collected_by"]), ("company", "ZenaTech"))

    def test_a_search_skipped_for_time_says_so(self):
        sources = []
        asyncio.run(sx._collect_watch(None, ls.build_queries({"company": ["ZenaTech"]}), sources, lambda: True, []))
        self.assertEqual(sources[0]["status"], "SKIPPED")

    def test_which_search_found_an_item_is_kept_with_it(self):
        feed = [{"external_id": "watch-company", "name": "Brand names", "comments": [
            {"text": "ZenaTech wins a contract", "url": "https://n/1", "category": "company", "collected_by": "ZenaTech"}]}]
        item = ce.feed_items(feed)[0]
        self.assertEqual((item.category, item.collected_by), ("company", "ZenaTech"))


if __name__ == "__main__":
    unittest.main()
