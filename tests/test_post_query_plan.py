"""The plan keeps only what the post supports, falls back without inventing, and splits direct from context."""

import asyncio
import unittest
from unittest.mock import AsyncMock, patch

from scripts import scrape_x_playwright as sx
from services.compliance.analytics import post_query_plan as plan

POST = "The U.S. Army has selected ZenaTech as its preferred drone technology partner for military operations."
RAW = {
    "subject": "ZenaTech selected by the U.S. Army",
    "organisations": ["U.S. Army", "ZenaTech", "Lockheed Martin"],  # the last is not in the post
    "direct_queries": ["ZenaTech U.S. Army drone partnership", "Lockheed Martin drones"],
    "contexts": [
        {"topic": "military drone use in conflict zones", "queries": ["military drones conflict zones", "drone warfare opinion", "third"],
         "terms": ["military drone", "drone warfare", "quantum computing"]},  # the last term shares nothing with the post
        {"topic": "defence drone procurement", "queries": ["army drone procurement"], "terms": ["drone procurement"]},
        {"topic": "Military Drone Use In Conflict Zones", "queries": ["dup"], "terms": ["military drone"]},  # same topic again
        {"topic": "quantum computing", "queries": ["quantum"], "terms": ["quantum computing"]},  # nothing from the post
        {"topic": "technology partners", "queries": ["technology partner news"], "terms": ["technology partner"]},
    ],
}


class ValidateTests(unittest.TestCase):
    def test_only_what_the_posts_text_supports_is_kept(self):
        v = plan.validate(RAW, POST)
        self.assertEqual(v["organisations"], ["U.S. Army", "ZenaTech"])
        self.assertEqual(v["queries"], ["ZenaTech U.S. Army drone partnership"])
        topics = [c["topic"] for c in v["contexts"]]
        self.assertEqual(topics, ["military drone use in conflict zones", "defence drone procurement", "technology partners"])
        first = v["contexts"][0]
        self.assertNotIn("quantum computing", first["terms"])
        self.assertEqual(len(first["queries"]), 2)  # two searches per wider topic at most

    def test_no_supported_context_term_means_no_wider_topic(self):
        raw = {**RAW, "contexts": [{"topic": "quantum", "queries": ["q"], "terms": ["quantum computing"]}]}
        self.assertEqual(plan.validate(raw, POST)["contexts"], [])

    def test_at_most_four_wider_topics(self):
        many = [{"topic": f"drone topic {i}", "queries": ["q"], "terms": ["military drone"]} for i in range(9)]
        self.assertEqual(len(plan.validate({**RAW, "contexts": many}, POST)["contexts"]), 4)

    def test_a_plan_saved_with_one_wider_topic_still_reads(self):
        old = {"context_topic": "t", "context_queries": ["q"], "context_terms": ["military drone"]}
        self.assertEqual(plan.contexts_of(old), [{"topic": "t", "queries": ["q"], "terms": ["military drone"]}])


class PlanTests(unittest.TestCase):
    def test_gemini_failure_falls_back_to_the_rules_and_says_why(self):
        with patch.object(plan, "read_cached", AsyncMock(return_value=None)), \
                patch.object(plan, "_ask_gemini", AsyncMock(side_effect=RuntimeError("429 quota"))):
            out = asyncio.run(plan.plan_for_text("Stacklens", "Meet Stacklens from ZenaTech", ""))
        self.assertEqual(out["source"], "rules")
        self.assertIn("quota", out["note"])
        self.assertEqual(out["contexts"], [])  # nothing invented to fill the gap

    def test_a_good_answer_is_saved_and_reused_without_asking_again(self):
        saved = {}

        async def get(key):
            return saved.get(key)

        async def put(key, value):
            saved[key] = value

        ask = AsyncMock(return_value=RAW)
        with patch.object(plan, "get_state", get), patch.object(plan, "_set_state", put), patch.object(plan, "_ask_gemini", ask):
            first = asyncio.run(plan.plan_for_text("Army", POST, ""))
            second = asyncio.run(plan.plan_for_text("Army", POST, ""))
        self.assertEqual(ask.await_count, 1)
        self.assertEqual(first["source"], "gemini")
        self.assertIn("saved", second["source"])

    def test_without_permission_to_ask_only_a_saved_plan_or_the_rules_are_used(self):
        ask = AsyncMock(return_value=RAW)
        with patch.object(plan, "read_cached", AsyncMock(return_value=None)), patch.object(plan, "_ask_gemini", ask):
            out = asyncio.run(plan.plan_for_text("Army", POST, "", allow_model=False))
        ask.assert_not_awaited()
        self.assertEqual(out["source"], "rules")


class ScraperSplitsDirectFromContextTests(unittest.TestCase):
    def test_direct_needs_every_organisation_but_context_needs_a_context_term(self):
        calls, pool, sources = [], [], []

        async def fake_news(page, query, limit=10, exclude_terms=None, drop_own_brand=True):
            calls.append(query)
            return [{"text": "ZenaTech wins Army drone deal"}, {"text": "Army tests new engine"},
                    {"text": "Military drone strikes debated"}, {"text": "Cooking tips"}], "SUCCESS"

        search = {"id": "x", "title": "T", "queries": ["direct q"],
                  "contexts": [{"topic": "military drone use", "queries": ["ctx q"], "terms": ["military drone"]},
                               {"topic": "cooking", "queries": ["ctx2"], "terms": ["cooking tips"]}],
                  "anchors": ["u.s army", "zenatech"],
                  "require_all": ["zenatech", "army"], "keywords": []}
        with patch.object(sx, "_scrape_live_trend_news_playwright", fake_news):
            topics = asyncio.run(sx._collect_post_searches(None, [search], sources, lambda: False, pool))
        by_topic = {}
        for p in pool:
            by_topic.setdefault(p["topic"], []).append(p["item"]["text"])
        self.assertEqual(by_topic["post-x"], ["ZenaTech wins Army drone deal"])  # the Army-only story is not direct
        self.assertEqual(by_topic["post-x-context-1"], ["Military drone strikes debated"])
        self.assertEqual(by_topic["post-x-context-2"], ["Cooking tips"])
        # single-word terms need two of the topic's words, not just one
        pool.clear()
        single = {**search, "contexts": [{"topic": "t", "queries": ["q"], "terms": ["drone", "military"]}]}

        async def news2(page, query, limit=10, exclude_terms=None, drop_own_brand=True):
            return [{"text": "Drone racing league announces dates"}, {"text": "Military drone budget grows"}], "SUCCESS"

        with patch.object(sx, "_scrape_live_trend_news_playwright", news2):
            asyncio.run(sx._collect_post_searches(None, [single], [], lambda: False, pool))
        self.assertEqual([p["item"]["text"] for p in pool if p["topic"] == "post-x-context-1"], ["Military drone budget grows"])  # each wider topic has its own filter
        self.assertEqual([t["external_id"] for t in topics], ["post-x", "post-x-context-1", "post-x-context-2"])
        self.assertEqual({s["kind"] for s in sources}, {"post:news", "post:context"})


class TopicScopingTests(unittest.TestCase):
    def test_every_wider_topic_of_a_post_belongs_to_that_post_only(self):
        from uuid import UUID
        from services.compliance.analytics import public_mood_service as pm

        me = UUID("11111111-1111-1111-1111-111111111111")
        mine = f"post-{me}"
        for ext in (mine, f"{mine}-context-1", f"{mine}-context-4"):
            self.assertTrue(pm._may_show_on({"external_id": ext}, me))
        self.assertFalse(pm._may_show_on({"external_id": "post-22222222-2222-2222-2222-222222222222-context-1"}, me))
        self.assertEqual(pm._topic_kind({"external_id": f"{mine}-context-2"}), "context")
        self.assertEqual(pm._topic_kind({"external_id": mine}), "own")


if __name__ == "__main__":
    unittest.main()
