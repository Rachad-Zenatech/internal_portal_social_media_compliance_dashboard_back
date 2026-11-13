"""The site lists that rank a post's sources come from settings, not code."""

import json
import os
import tempfile
import unittest
from unittest import mock

from services.compliance.evaluation import source_settings
from services.compliance.evaluation.source_check_service import authority_of


class SourceSettingsTests(unittest.TestCase):
    def tearDown(self):
        source_settings.reload()

    def test_default_file_ranks_sites(self):
        with mock.patch.dict(os.environ, {}, clear=False):
            for var in source_settings._LIST_ENV.values():
                os.environ.pop(var, None)
            os.environ.pop("SOURCE_SITES_FILE", None)
            source_settings.reload()
            self.assertEqual(authority_of("https://www.sec.gov/x"), "OFFICIAL")
            self.assertEqual(authority_of("https://army.mil/x"), "OFFICIAL")
            self.assertEqual(authority_of("https://www.businesswire.com/x"), "COMPANY")
            self.assertEqual(authority_of("https://www.reuters.com/x"), "NEWS")
            self.assertEqual(authority_of("https://someblog.example/x"), "OTHER")

    def test_environment_replaces_a_list(self):
        with mock.patch.dict(os.environ, {"SOURCE_NEWS_SITES": "someblog.example, WWW.Other.example",
                                          "SOURCE_COMPANY_SITES": "acme.example"}):
            source_settings.reload()
            self.assertEqual(authority_of("https://someblog.example/x"), "NEWS")
            self.assertEqual(authority_of("https://news.other.example/x"), "NEWS")
            self.assertEqual(authority_of("https://www.reuters.com/x"), "OTHER")
            self.assertEqual(authority_of("https://acme.example/news"), "COMPANY")

    def test_other_file_and_missing_file(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "sites.json")
            with open(path, "w", encoding="utf-8") as f:
                json.dump({"official_sites": ["regulator.example"]}, f)
            with mock.patch.dict(os.environ, {"SOURCE_SITES_FILE": path}):
                for var in source_settings._LIST_ENV.values():
                    os.environ.pop(var, None)
                source_settings.reload()
                self.assertEqual(authority_of("https://regulator.example/r"), "OFFICIAL")
                self.assertEqual(authority_of("https://www.reuters.com/x"), "OTHER")
            with mock.patch.dict(os.environ, {"SOURCE_SITES_FILE": os.path.join(d, "missing.json")}):
                source_settings.reload()
                self.assertEqual(authority_of("https://www.sec.gov/x"), "OTHER")


if __name__ == "__main__":
    unittest.main()
