"""Image scans use HTTPS www links while retaining article paths and existing subdomains."""
import unittest
from unittest.mock import AsyncMock, patch

from models.compliance.source_link_model import ImageSourceCitation
from services.compliance.submissions import image_source_service as scans
from services.compliance.submissions.source_link_service import normalize_scanned_source_url


class ScannedSourceUrlTests(unittest.TestCase):
    def test_root_domains_use_https_www_without_changing_article_location(self):
        for raw, expected in (
            ("https://army.mil/article/295038", "https://www.army.mil/article/295038"),
            ("army.mil/article/295038?ref=image#section", "https://www.army.mil/article/295038?ref=image#section"),
            ("http://www.army.mil/article/295038", "https://www.army.mil/article/295038"),
            ("https://example.co.uk/article", "https://www.example.co.uk/article"),
            ("https://news.army.mil/article", "https://news.army.mil/article"),
        ):
            with self.subTest(raw=raw):
                self.assertEqual(normalize_scanned_source_url(raw), expected)


class ImageSourceCandidateTests(unittest.IsolatedAsyncioTestCase):
    async def test_printed_url_gets_www(self):
        citation = ImageSourceCitation(kind="URL", reference="https://army.mil/article/295038")
        self.assertEqual(await scans._candidate_urls([citation]), ["https://www.army.mil/article/295038"])

    async def test_search_results_get_www_and_are_deduplicated(self):
        citation = ImageSourceCitation(kind="NAMED", reference="Army article")
        results = ["https://army.mil/article/295038", "https://www.army.mil/article/295038"]
        with patch.object(scans, "find_source_urls_on_web", AsyncMock(return_value=results)):
            self.assertEqual(await scans._candidate_urls([citation]), ["https://www.army.mil/article/295038"])
