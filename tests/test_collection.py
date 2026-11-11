"""Scraped numbers are only what was collected; collection is broad but bounded; failures are not 'no results'."""

import asyncio
import unittest
from unittest.mock import patch

from scripts import scrape_x_playwright as sx
from services.compliance.analytics import listening_providers as lp

DELIVERY = next(t for t in sx.TRENDING_MARKET_TOPICS if "delivery" in t["external_id"])


class NoInventedNumbersTests(unittest.TestCase):
    def _analyze(self, comments, **extra):
        return lp.analyze_raw_x_topic({"comments": comments, **extra}, days=7)

    def test_volume_is_the_number_collected_and_nothing_is_estimated(self):
        out = self._analyze([{"text": "Great drone news", "network": "X"}, {"text": "The company released a product"}])
        self.assertEqual(out["volume"], 2)
        self.assertIsNone(out["volume_change_pct"])
        self.assertEqual(out["daily_volume"], [])

    def test_no_items_means_zeros_not_an_invented_mood(self):
        out = self._analyze([])
        self.assertEqual((out["positive"], out["neutral"], out["negative"], out["volume"]), (0.0, 0.0, 0.0, 0))

    def test_dates_and_figures_the_source_gave_are_kept_and_missing_ones_stay_missing(self):
        out = self._analyze([{"text": "Delivery drones expand", "posted_on": "2026-11-09", "engagements": 12},
                             {"text": "Another headline"}])
        by_text = {p["text"]: p for p in out["top_posts"]}
        self.assertEqual(by_text["Delivery drones expand"]["posted_on"], "2026-11-09")
        self.assertEqual(by_text["Delivery drones expand"]["engagements"], 12)
        self.assertIsNone(by_text["Another headline"]["posted_on"])
        self.assertEqual(by_text["Another headline"]["engagements"], 0)

    def test_figures_a_source_does_report_are_used(self):
        daily = [5, 5, 5, 6, 8, 10, 3]  # the last entry is today, still filling up
        out = self._analyze([{"text": "x headline"}], volume=5000, daily_volume=daily)
        self.assertEqual(out["volume"], 5000)
        self.assertEqual(out["daily_volume"], daily)
        self.assertEqual(out["volume_change_pct"], 60.0)  # last three complete days (24) against the three before (15)

    def test_a_small_sample_is_not_treated_as_enough_evidence(self):
        rec, _ = lp.evaluate_publishing_decision(positive_pct=100, neutral_pct=0, negative_pct=0, volume=3)
        self.assertEqual(rec, "WAIT")


class CandidateTests(unittest.TestCase):
    def test_post_using_different_wording_is_still_a_candidate(self):
        # No topic phrase ("drone delivery", "last mile") appears, but its words do.
        self.assertTrue(sx.is_candidate("New autonomous drones will deliver parcels to rural homes", DELIVERY))

    def test_unrelated_post_is_not(self):
        self.assertFalse(sx.is_candidate("Quarterly earnings beat expectations for retail banks", DELIVERY))

    def test_exclusion_terms_win(self):
        cfg = dict(DELIVERY, exclude_terms=["toy"])
        self.assertFalse(sx.is_candidate("Toy drone delivery set for kids", cfg))
        with patch.dict("os.environ", {"X_COLLECT_EXCLUDE_TERMS": "giveaway"}):
            self.assertFalse(sx.is_candidate("Drone delivery giveaway, enter now", DELIVERY))

    def test_extra_collection_terms_widen_the_net(self):
        text = "Quadcopter parcel trials begin"
        self.assertFalse(sx.is_candidate(text, DELIVERY))
        self.assertTrue(sx.is_candidate(text, dict(DELIVERY, collect_terms=["quadcopter"])))


class StatusTests(unittest.TestCase):
    def _src(self, *statuses):
        return [{"kind": "k", "name": str(i), "status": s, "items": 0} for i, s in enumerate(statuses)]

    def test_all_good_is_success(self):
        self.assertEqual(sx.overall_status(self._src("SUCCESS", "SUCCESS"), 10, False), "SUCCESS")

    def test_empty_source_is_not_a_problem(self):
        self.assertEqual(sx.overall_status(self._src("SUCCESS", "EMPTY"), 10, False), "SUCCESS")

    def test_a_blocked_or_failed_source_makes_it_partial(self):
        for bad in ("AUTH_REQUIRED", "RATE_LIMITED", "FAILED", "SKIPPED"):
            self.assertEqual(sx.overall_status(self._src("SUCCESS", bad), 10, False), "PARTIAL", bad)

    def test_hitting_the_time_limit_is_partial(self):
        self.assertEqual(sx.overall_status(self._src("SUCCESS"), 10, True), "PARTIAL")

    def test_failure_is_told_apart_from_no_results(self):
        self.assertEqual(sx.overall_status(self._src("FAILED", "FAILED"), 0, False), "FAILED")
        self.assertEqual(sx.overall_status(self._src("AUTH_REQUIRED", "EMPTY"), 0, False), "AUTH_REQUIRED")
        self.assertEqual(sx.overall_status(self._src("RATE_LIMITED"), 0, False), "RATE_LIMITED")
        self.assertEqual(sx.overall_status(self._src("EMPTY", "EMPTY"), 0, False), "EMPTY")

    def test_page_status_reads_the_login_wall_and_rate_limits(self):
        self.assertEqual(sx._page_status("Log in to X to see posts", 0), "AUTH_REQUIRED")
        self.assertEqual(sx._page_status("429 Too Many Requests", 0), "RATE_LIMITED")
        self.assertEqual(sx._page_status("", 0), "EMPTY")
        self.assertEqual(sx._page_status("Log in", 3), "SUCCESS")


if __name__ == "__main__":
    unittest.main()


def _snowflake(when):
    """An X post id for a given time (the inverse of sx.snowflake_time)."""
    return str((int(when.timestamp() * 1000) - 1288834974657) << 22)


def _profile(handle, posts):
    """A rendered profile in the two layouts seen: with and without an empty link before the author header."""
    page = f"Title: x\n\n## {handle}'s posts\n\n"
    for i, (post_id, text, layout) in enumerate(posts):
        head = f"*   [](https://x.com/{handle}/status/{post_id})" if layout == "empty-link" else "*   "
        if layout == "plain":
            head = "*   [![Image: @a](https://p/x.jpg)](https://x.com/" + handle + ")"
        page += (f"\n{head}[![Image {i}: @{handle}](https://p/x.jpg)](https://x.com/{handle})  [Name](https://x.com/{handle}) "
                 f"[@{handle}](https://x.com/{handle}) [Nov 10](https://x.com/{handle}/status/{post_id})   {text}  "
                 f"![Image](https://pbs.twimg.com/media/a?format=webp)  [](https://x.com/{handle}/status/{post_id}/photo/1)   "
                 f"[](https://x.com/compose/post?in_reply_to={post_id}) 3 20 585 ?\n")
    return page


class XProfileExtractionTests(unittest.TestCase):
    NOW = None

    def setUp(self):
        from datetime import datetime, timedelta, timezone
        self.now = datetime.now(timezone.utc)
        self.recent = _snowflake(self.now - timedelta(days=1))
        self.old = _snowflake(self.now - timedelta(days=900))

    def test_a_post_id_gives_its_real_time(self):
        from datetime import timedelta
        got = sx.snowflake_time(self.recent)
        self.assertLess(abs((got - (self.now - timedelta(days=1))).total_seconds()), 1)

    def test_text_dates_and_links_are_read_in_both_layouts_with_no_invented_engagement(self):
        body = _profile("Kratos", [
            (self.recent, "To every veteran who has stood watch, thank you. Your dedication inspires us.", "empty-link"),
            (self.old, "An older announcement about a contract that was awarded some time ago.", "plain"),
        ])
        posts = sx._extract_tweets_from_rendered_profile(body, "Kratos")
        self.assertEqual(len(posts), 2)
        self.assertEqual(posts[0]["text"], "To every veteran who has stood watch, thank you. Your dedication inspires us.")
        self.assertEqual(posts[0]["url"], f"https://x.com/Kratos/status/{self.recent}")
        self.assertEqual(posts[0]["engagements"], 0)  # the page's digits are dates and other posts' counts, not ours
        self.assertEqual(posts[0]["published_at"][:4], str(self.now.year))
        self.assertTrue(posts[1]["published_at"].startswith(str(self.now.year - 2)))
        for p in posts:
            self.assertNotIn("@Kratos", p["text"])  # no author header leaking into the text

    def test_old_posts_on_a_profile_are_not_treated_as_current(self):
        from unittest.mock import AsyncMock, MagicMock
        body = _profile("Kratos", [(self.recent, "Fresh post about our new product line launching.", "empty-link"),
                                   (self.old, "Pinned post from years ago about a past partnership.", "plain")])
        page = MagicMock()
        page.goto = AsyncMock()
        page.inner_text = AsyncMock(return_value=body)
        posts, status = asyncio.run(sx._scrape_x_handle_playwright(page, "Kratos"))
        self.assertEqual([p["text"][:5] for p in posts], ["Fresh"])
        self.assertEqual(status, "SUCCESS")

    def test_a_profile_with_only_old_posts_is_empty_not_a_success(self):
        from unittest.mock import AsyncMock, MagicMock
        page = MagicMock()
        page.goto = AsyncMock()
        page.inner_text = AsyncMock(return_value=_profile("Kratos", [(self.old, "Only an old post about something from long ago.", "plain")]))
        posts, status = asyncio.run(sx._scrape_x_handle_playwright(page, "Kratos"))
        self.assertEqual((posts, status), ([], "EMPTY"))
