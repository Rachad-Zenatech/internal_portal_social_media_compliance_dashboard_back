"""Public mood never shows invented numbers: with no listening source it shows that there is none."""

import asyncio
import inspect
import os
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from services.compliance.analytics import listening_providers as lp
from services.compliance.analytics import public_mood_service as pms


def run(coro):
    return asyncio.run(coro)


class NoSourceTests(unittest.TestCase):
    def test_with_no_source_there_is_a_provider_that_has_no_data_and_says_so(self):
        p = lp.UnconfiguredProvider()
        self.assertEqual((p.name, p.is_mock), ("unconfigured", False))
        for call in (p.list_topics(), p.snapshot("anything")):
            with self.assertRaises(lp.ListeningUnavailable) as cm:
                run(call)
            self.assertIn("No listening source is configured", str(cm.exception))

    def test_no_database_and_no_feed_file_means_no_source_never_sample_data(self):
        lp.reset_provider_cache()
        try:
            fake_store = type("S", (), {"is_enabled": staticmethod(lambda: False)})()
            with patch.object(lp, "_x_feed_store", return_value=fake_store), patch.dict(os.environ, {"SOCIAL_LISTENING_PROVIDER": ""}), \
                    patch.object(lp, "DEFAULT_X_JSON_PATH", Path("no-such-feed.json")):
                self.assertEqual(lp.get_provider().name, "unconfigured")
        finally:
            lp.reset_provider_cache()

    def test_the_topic_list_is_empty_not_invented_when_there_is_no_source(self):
        with patch.object(pms, "get_provider", return_value=lp.UnconfiguredProvider()), \
                patch.object(pms, "fetch_one", AsyncMock(return_value=None)), patch.object(pms, "fetch_all", AsyncMock(return_value=[])), \
                patch.object(pms, "_load_topics", AsyncMock(return_value=[])):
            self.assertEqual(run(pms.list_topics()), [])

    def test_the_source_is_labelled_for_what_it_is(self):
        self.assertEqual(pms._source_label("x_json"), "X (Twitter)")
        self.assertEqual(pms._source_label("sprout"), "Sprout Social")
        self.assertEqual(pms._source_label("unconfigured"), "No listening source")

    def test_the_sample_data_code_is_gone(self):
        self.assertFalse(hasattr(lp, "MockSproutProvider"))
        self.assertFalse(hasattr(lp, "_MOCK_TOPICS"))
        source = inspect.getsource(lp)
        self.assertNotIn("random.", source)  # nothing in the listening code can generate a number
        self.assertNotIn("mock-", source)


if __name__ == "__main__":
    unittest.main()
