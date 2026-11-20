"""Repeated source credits resolve to one source without hiding unrelated or unreadable reports."""
import unittest
from types import SimpleNamespace
from models.compliance.claim_manifest_model import ClaimManifest
from unittest.mock import AsyncMock, patch

from services.compliance.evaluation import citation_finder as cf, source_identity as identity, source_check_service as sc
from services.compliance.evaluation.source_fetch_service import FetchedSource

URL = "https://army.mil/article/295038"
IMAGE = "Text in image 1: Tethered drones.\nSource: U.S. Army / September 2, 2026 / army.mil/article/295038\nTwo weeks describes the assessment, not continuous flight duration."


class SourceIdentityTests(unittest.TestCase):
    def test_image_credit_and_attached_url_are_one_source(self):
        found = cf.find(IMAGE + "\n\nSources:\n" + URL)
        self.assertEqual(len(found), 1)
        self.assertEqual((found[0].kind, found[0].where), ("URL", "source_links"))
        self.assertEqual(found[0].reference, URL)

    def test_credit_with_a_url_does_not_create_a_name_only_fragment(self):
        found = cf.find(IMAGE)
        self.assertEqual([(c.kind, c.reference) for c in found], [("URL", URL)])

    def test_caption_and_image_url_variants_share_identity(self):
        found = cf.find("Source: http://www.example.com/report/123/?utm_source=post#page2\n\nSources:\nhttps://example.com/report/123")
        self.assertEqual(len(found), 1)
        self.assertEqual(found[0].where, "source_links")

    def test_distinct_report_identifiers_and_paths_remain_separate(self):
        for first, second in [("https://example.com/report?id=1", "https://example.com/report?id=2"),
                              ("https://example.com/report/123", "https://example.com/report/456")]:
            with self.subTest(first=first):
                self.assertEqual(len(cf.find("Source: " + first + "\n\nSources:\n" + second)), 2)

    def test_one_linked_source_does_not_remove_another_named_source(self):
        found = cf.find("Sources: Research agency report https://example.com/report/123; Gartner Hype Cycle 2026")
        self.assertEqual({c.kind for c in found}, {"URL", "NAMED"})
        self.assertEqual(next(c.reference for c in found if c.kind == "NAMED"), "Gartner Hype Cycle 2026")

    def test_wrapped_url_completes_its_source_credit_without_absorbing_the_note(self):
        found = cf.find("Text in image 1: Source: Research Agency / September 2, 2026 /\nhttps://example.com/report/123\nIllustration only.")
        self.assertEqual([(c.kind, c.reference) for c in found], [("URL", "https://example.com/report/123")])

    def test_a_matching_full_link_replaces_only_the_truncated_citation(self):
        found = cf.find("Read the report https://example.com/reports/annual.../\n\nSources:\nhttps://example.com/reports/annual-2026")
        self.assertEqual(len(found), 1)
        self.assertFalse(found[0].truncated)

    def test_unrelated_or_ambiguous_full_links_do_not_hide_a_truncated_source(self):
        for attached in ["https://example.com/reports/different", "https://example.com/reports/annual-2025\nhttps://example.com/reports/annual-2026"]:
            found = cf.find("Read the report https://example.com/reports/annual.../\n\nSources:\n" + attached)
            self.assertTrue(any(c.truncated for c in found))

    def test_identical_publishers_do_not_merge_different_report_urls(self):
        checks = [SimpleNamespace(kind="URL", reference=url, url=url) for url in
                  ["https://army.mil/article/111", "https://army.mil/article/222"]]
        self.assertEqual(len(identity.readings([(c, ("same title", "same publisher")) for c in checks])), 2)


class SourceResolutionTests(unittest.IsolatedAsyncioTestCase):
    async def check(self, text, fetched=None):
        fetch = AsyncMock(side_effect=fetched) if callable(fetched) else AsyncMock(return_value=fetched or FetchedSource(
            url=URL, ok=True, status="OK", title="Soldiers, developers assess tethered drones", text="The report describes a two-week assessment of tethered drones.", content_hash="test"))
        compare = AsyncMock(return_value=([], "test-model", False))
        with patch.object(sc.claim_manifest, "get", AsyncMock(return_value=ClaimManifest(version="test", revision="test"))), patch.object(sc, "ENABLED", True), \
             patch.object(sc.newsroom_facts, "material", AsyncMock(return_value=None)), \
             patch.object(sc.source_fetch_service, "fetch", fetch), \
             patch.object(sc.source_post_checks, "compare_parts", compare):
            result = await sc.run(text)
        return result, fetch, compare

    async def test_same_image_and_attached_source_produce_no_phantom_warning(self):
        result, fetch, compare = await self.check(IMAGE + "\n\nSources:\n" + URL)
        self.assertEqual(len(result.checks), 1)
        self.assertEqual(result.checks[0].status, "READ")
        self.assertEqual(sc.findings(result.checks), [])
        fetch.assert_awaited_once_with(URL)
        compare.assert_awaited_once()

    async def test_an_image_url_is_read_without_requiring_duplicate_attachment(self):
        result, fetch, _ = await self.check(IMAGE)
        self.assertEqual([(c.kind, c.status, c.where) for c in result.checks], [("URL", "READ", "image")])
        fetch.assert_awaited_once_with(URL)

    async def test_a_real_fetch_failure_remains_one_warning(self):
        result, fetch, compare = await self.check(IMAGE + "\n\nSources:\n" + URL,
            FetchedSource(url=URL, ok=False, status="NOT_FOUND", error="Source unavailable."))
        self.assertEqual(len(result.checks), 1)
        self.assertEqual(result.checks[0].status, "UNREADABLE")
        self.assertEqual(len(sc.findings(result.checks)), 1)
        compare.assert_not_awaited()

    async def test_distinct_sources_are_both_read(self):
        result, fetch, compare = await self.check(IMAGE + "\n\nSources:\nhttps://army.mil/article/999999")
        self.assertEqual(len(result.checks), 2)
        self.assertEqual(fetch.await_count, 2)
        self.assertEqual(compare.await_count, 2)

    async def test_redirect_aliases_are_compared_once(self):
        def fetched(url):
            return FetchedSource(url=url, final_url="https://www.example.com/report/final", ok=True, status="OK", title="Report", text="A report about tethered drone assessment.")
        result, fetch, compare = await self.check("Sources:\nhttps://example.com/report/old\nhttps://www.example.com/report/final", fetched)
        self.assertEqual(len(result.checks), 1)
        self.assertEqual(fetch.await_count, 2)
        compare.assert_awaited_once()
