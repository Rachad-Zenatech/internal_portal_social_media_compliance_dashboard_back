"""The process log reports what was done, and only that."""

import unittest

from services.compliance.analytics import public_mood_process as pp


class StepTests(unittest.TestCase):
    def test_what_was_read_and_that_attachments_are_not(self):
        s = pp.read_step({"title": "T", "caption_text": "", "script_text": "body", "transcript_text": "", "text": "T\nbody"})
        self.assertIn("script: 4 characters", s["lines"])
        self.assertTrue(any("Other attached documents" in l for l in s["lines"]))

    def test_no_search_is_said_plainly(self):
        self.assertEqual(pp.search_step({"queries": [], "anchors": []})["status"], "none")

    def test_scrape_step_reports_found_dropped_and_kept_for_this_posts_searches(self):
        feed = {"collected_at": "2026-01-01T00:00:00+00:00", "collection": {"sources": [
            {"kind": "post:news", "name": "q1", "status": "SUCCESS", "items": 10, "dropped_unrelated": 7},
            {"kind": "post:news", "name": "other", "status": "SUCCESS", "items": 99}]}}
        s = pp.scrape_step(feed, ["q1"])
        self.assertIn("3 kept", s["lines"][0])
        self.assertEqual(s["status"], "ok")

    def test_a_search_that_was_not_in_the_last_scrape_says_to_scrape(self):
        s = pp.scrape_step({"collected_at": "2026-01-01T00:00:00+00:00", "collection": {"sources": []}}, ["q1"])
        self.assertEqual(s["status"], "warn")
        self.assertIn("Click Scrape X", s["lines"][0])

    def test_no_feed_means_nothing_collected(self):
        self.assertEqual(pp.scrape_step(None, ["q"])["status"], "none")


if __name__ == "__main__":
    unittest.main()
