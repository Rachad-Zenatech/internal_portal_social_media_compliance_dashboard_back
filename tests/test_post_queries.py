"""Searches come from what each draft says, not from a fixed topic list."""

import asyncio
import json
import os
import unittest
from unittest.mock import AsyncMock, patch

from scripts import scrape_x_playwright as sx
from services.compliance.analytics import post_queries as pq


class QueryBuildingTests(unittest.TestCase):
    def test_queries_use_the_drafts_own_names_and_hashtags(self):
        out = pq.queries_for_text("ZenaDrone delivers aid in Texas", "Built with the FAA. #CombatProven", "")
        joined = " ".join(out["queries"]).lower()
        self.assertIn("zenadrone", joined)
        self.assertIn("combat proven", joined)

    def test_a_draft_that_names_nothing_gets_no_search(self):
        self.assertEqual(pq.queries_for_text("Happy Friday", "have a great weekend", "")["queries"], [])

    def test_different_drafts_give_different_searches(self):
        a = pq.queries_for_text("Joby Aviation tests air taxis", "", "")["queries"]
        b = pq.queries_for_text("ZenaDrone inspects solar farms", "", "")["queries"]
        self.assertNotEqual(a, b)

    def test_the_searches_survive_the_hand_off_to_the_scraper(self):
        search = {"id": "abc", "title": "T", **pq.queries_for_text("ZenaDrone in Texas", "", "")}
        path = pq.write_file([search])
        try:
            self.assertEqual(pq.read_file(path)[0]["id"], "abc")
        finally:
            os.remove(path)
        self.assertEqual(pq.read_file("no-such-file.json"), [])
        self.assertIsNone(pq.write_file([]))

    def test_the_draft_topic_is_matched_back_by_its_keywords(self):
        t = pq.topic_for_draft({"id": "abc", "title": "T", "queries": ["x"], "keywords": ["zenadrone"]})
        self.assertEqual((t["external_id"], t["category"], t["keywords"]), ("post-abc", "post", ["zenadrone"]))


class NotUnrelatedTests(unittest.TestCase):
    def test_everyday_words_never_become_a_search(self):
        for title, caption in [("Test Script", "Automate #digitaltransformation with AI-powered automation"),
                               ("test", "Innovation in Cybersecurity SaaS"), ("Jadian", "American defense and drone technology")]:
            out = pq.queries_for_text(title, caption, "")
            for q in out["queries"]:
                self.assertNotIn(q.strip('"'), {"innovation", "automation", "defense", "american", "cybersecurity", "saas", "test script"})
            self.assertNotIn("test script", out["queries"])

    def test_a_brand_phrase_and_a_name_still_search(self):
        out = pq.queries_for_text("Stacklens", "Meet Stacklens from ZenaTech. #DroneAsAService", "")
        self.assertIn('"drone as a service"', out["queries"])
        self.assertIn("stacklens", out["anchors"])

    def test_the_scraper_drops_headlines_that_do_not_mention_the_drafts_names(self):
        self.assertTrue(sx.mentions_any("Stacklens launches new glasses - PCMag", ["stacklens"]))
        self.assertFalse(sx.mentions_any("Best Smart Glasses 2026 ranked", ["stacklens"]))
        self.assertFalse(sx.mentions_any("anything", []))

    def test_unrelated_search_results_are_not_kept(self):
        pool, sources = [], []

        async def fake_news(page, query, limit=10, exclude_terms=None, drop_own_brand=True):
            return [{"text": "Stacklens ships"}, {"text": "Walmart ramps up automation"}], "SUCCESS"

        search = {"id": "a", "title": "T", "queries": ["q"], "keywords": ["stacklens"], "anchors": ["stacklens"]}
        with patch.object(sx, "_scrape_live_trend_news_playwright", fake_news):
            asyncio.run(sx._collect_post_searches(None, [search], sources, lambda: False, pool))
        self.assertEqual([p["item"]["text"] for p in pool], ["Stacklens ships"])
        self.assertEqual(sources[0]["dropped_unrelated"], 1)


class OnlyThisDraftsTopicTests(unittest.TestCase):
    def test_a_drafts_own_search_shows_only_on_that_draft(self):
        from services.compliance.analytics import public_mood_service as pms
        mine, other = "11111111-1111-1111-1111-111111111111", "22222222-2222-2222-2222-222222222222"
        self.assertTrue(pms._may_show_on({"external_id": f"post-{mine}"}, mine))
        self.assertFalse(pms._may_show_on({"external_id": f"post-{other}"}, mine))
        self.assertTrue(pms._may_show_on({"external_id": "watch-company"}, mine))


class ReadingDraftsTests(unittest.TestCase):
    def test_drafts_are_read_with_a_json_transcript_and_a_script_without_a_type_error(self):
        rows = [{"id": "1", "title": "ZenaDrone in Texas", "caption_text": "", "script_body": None, "script_file_text": None,
                 "transcript": json.dumps([{"text": "Joby Aviation tests"}])},
                {"id": "2", "title": "Happy Friday", "caption_text": "", "script_body": "have a nice day", "script_file_text": None, "transcript": None}]
        from services.compliance.analytics import post_query_plan as plan
        with patch.object(pq, "fetch_all", AsyncMock(return_value=rows)),                 patch.object(plan, "read_cached", AsyncMock(return_value=None)),                 patch.object(plan, "_ask_gemini", AsyncMock(side_effect=RuntimeError("quota"))):
            out = asyncio.run(pq.active_draft_searches())
        self.assertEqual([d["id"] for d in out], ["1"])
        self.assertIn("ZenaDrone", pq.combine_body(None, "ZenaDrone file text", None))
        self.assertEqual(pq.combine_body("same", "same", None), "same")  # a repeated block is read once
        self.assertIn("joby aviation", out[0]["keywords"])  # the transcript was read too

    def test_script_template_labels_are_not_searched(self):
        self.assertEqual(pq.queries_for_text("test", "", "Hook: Voiceover: Headline:")["queries"], [])


class ScraperUsesThemTests(unittest.TestCase):
    def test_a_search_runs_for_every_query_of_the_draft_and_files_items_under_its_topic(self):
        seen, pool, sources = [], [], []

        async def fake_news(page, query, limit=10, exclude_terms=None, drop_own_brand=True):
            seen.append(query)
            return [{"text": f"ZenaDrone story for {query}"}], "OK"

        search = {"id": "abc", "title": "T", "queries": ["q one", "q two"], "keywords": ["zenadrone"], "anchors": ["zenadrone"]}
        with patch.object(sx, "_scrape_live_trend_news_playwright", fake_news):
            topics = asyncio.run(sx._collect_post_searches(None, [search], sources, lambda: False, pool))
        self.assertEqual(seen, ["q one", "q two"])
        self.assertEqual([t["external_id"] for t in topics], ["post-abc"])
        self.assertEqual({p["topic"] for p in pool}, {"post-abc"})
        self.assertEqual([s["items"] for s in sources], [1, 1])

    def test_the_fixed_market_topics_are_off_unless_asked_for(self):
        self.assertFalse(sx.FIXED_TOPICS_ON)


if __name__ == "__main__":
    unittest.main()
