"""The newer source-check safeguards: evidence has to be in the source, the browser fallback for pages the plain fetch
can't read, pasted article text, and checking a claim against more than one source. No network, no AI, no browser."""

import unittest
from unittest.mock import AsyncMock, patch

from models.compliance.compliance_model import SourceCheck, SourceClaim
from services.compliance.evaluation import source_browser_fetch as bf
from services.compliance.evaluation import source_check_service as sc
from services.compliance.evaluation import source_fetch_service as sf
from services.compliance.submissions import image_source_checks as isc
from services.compliance.evaluation import source_passages as sp

sp.EMBED_ENABLED = False  # keywords only: no embedding calls in tests

# A long press release: the figure that matters sits deep inside, after the first cut-off would have been.
FILLER = "The company continues to invest in its platforms and its people across several regions. " * 12
RELEASE = (
    "ZenaTech Reports Q2 2026 Results\n"
    "All financial figures are reported in Canadian dollars unless otherwise noted.\n"
    + "\n".join(FILLER for _ in range(40)) + "\n"
    "Second Quarter Financial Overview\n"
    "The DaaS segment contributed $8.1 million of revenue for the second quarter of 2026. Total revenue was $9.3 million.\n"
    + "\n".join(FILLER for _ in range(10))
)

MEMO = ("Project Agincourt: Pathway to the Autonomous Warfare Command. DRPM-UxS will collaborate directly with Congress "
        "to secure necessary legislation with a target establishment date of October 1, 2027. Within 30 days of this "
        "memorandum, DRPM-UxS will submit a plan of action and milestones (POAM) for my approval. Initial work will "
        "proceed immediately under existing authorities. " * 2)


def result(claim, evidence, verdict="SUPPORTED", confidence=0.95):
    return sc._Result(claim=claim, verdict=verdict, evidence=evidence, confidence=confidence)


class EvidenceTests(unittest.TestCase):
    def test_evidence_taken_from_the_source_keeps_the_claim_supported(self):
        out = sc._to_claims([result("OCT 2027 target date", "a target establishment date of October 1, 2027")], MEMO)
        self.assertEqual(out[0].verdict, "SUPPORTED")

    def test_a_paraphrase_in_the_sources_words_is_accepted(self):
        out = sc._to_claims([result("Runs on existing authorities",
                                    "The memo states that initial work will proceed under existing authorities.")], MEMO)
        self.assertEqual(out[0].verdict, "SUPPORTED")

    def test_evidence_the_source_never_says_becomes_unclear(self):
        out = sc._to_claims([result("Congress approved the command",
                                    "Lawmakers unanimously ratified funding legislation yesterday afternoon")], MEMO)
        self.assertEqual(out[0].verdict, "UNCLEAR")
        self.assertIn("couldn't be found", out[0].note)

    def test_short_evidence_is_not_judged_this_way(self):
        self.assertEqual(sc.evidence_overlap("the source says so", MEMO), 1.0)


class BrowserFallbackTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        sf._cache.clear()

    async def test_a_walled_page_is_tried_in_the_browser(self):
        walled = sf.FetchedSource(url="u", ok=False, status="FORBIDDEN", error="walled")
        read = sf.FetchedSource(url="u", ok=True, status="OK", text="x" * 300)
        with patch.object(sf, "_fetch", new=AsyncMock(return_value=walled)), \
                patch.object(bf, "fetch", new=AsyncMock(return_value=read)) as browser:
            out = await sf.fetch("https://example.com/walled")
        browser.assert_awaited_once()
        self.assertTrue(out.ok)

    async def test_when_the_browser_fails_too_the_first_reason_is_kept(self):
        walled = sf.FetchedSource(url="u", ok=False, status="FORBIDDEN", error="walled")
        with patch.object(sf, "_fetch", new=AsyncMock(return_value=walled)), \
                patch.object(bf, "fetch", new=AsyncMock(return_value=None)):
            out = await sf.fetch("https://example.com/walled2")
        self.assertEqual((out.ok, out.error), (False, "walled"))

    async def test_a_missing_page_or_a_refused_address_is_not_retried(self):
        for status in ("NOT_FOUND", "BLOCKED"):
            sf._cache.clear()
            failed = sf.FetchedSource(url="u", ok=False, status=status, error="no")
            with patch.object(sf, "_fetch", new=AsyncMock(return_value=failed)), \
                    patch.object(bf, "fetch", new=AsyncMock()) as browser:
                await sf.fetch(f"https://example.com/{status}")
            with self.subTest(status=status):
                browser.assert_not_awaited()

    async def test_the_browser_refuses_private_addresses(self):
        with patch.object(bf, "ENABLED", True):
            self.assertIsNone(await bf.fetch("http://127.0.0.1/admin"))


def check(url, *claims, authority="OTHER"):
    return SourceCheck(reference=url, kind="URL", status="READ", url=url, title=url, authority=authority,
                       claims=list(claims))


def claim(text, verdict, evidence=None):
    return SourceClaim(claim=text, verdict=verdict, evidence=evidence, confidence=0.9)


class SeveralSourcesTests(unittest.TestCase):
    def test_official_sources_come_first(self):
        urls = ["https://blog.example/a", "https://www.reuters.com/b", "https://www.war.gov/c"]
        self.assertEqual(isc.rank_by_authority(urls)[0], "https://www.war.gov/c")
        self.assertEqual(isc.rank_by_authority(urls)[1], "https://www.reuters.com/b")

    def test_a_claim_the_first_source_lacks_is_backed_by_the_second(self):
        first = check("https://a.example", claim("30 days to submit a plan of action", "NOT_FOUND"))
        second = check("https://b.example", claim("30 days to submit a plan of action and milestones", "SUPPORTED",
                                                  "Within 30 days... plan of action and milestones"))
        merged = isc.merge_checks(first, [second])
        self.assertEqual(merged.claims[0].verdict, "SUPPORTED")
        self.assertIn("https://b.example", merged.claims[0].note)
        self.assertEqual(merged.claims[0].source_url, "https://b.example")  # its link goes to the page that says it

    def test_a_contradiction_is_never_overruled(self):
        first = check("https://a.example", claim("Command set up by OCT 2027", "CONTRADICTED"))
        second = check("https://b.example", claim("Command set up by OCT 2027", "SUPPORTED", "by Oct. 1, 2027"))
        merged = isc.merge_checks(first, [second])
        self.assertEqual(merged.claims[0].verdict, "CONTRADICTED")
        self.assertIn("https://b.example", merged.claims[0].note)

    def test_a_claim_only_the_second_source_covers_is_added(self):
        first = check("https://a.example", claim("OCT 2027 target date", "SUPPORTED", "October 1, 2027"))
        second = check("https://b.example", claim("Work runs on existing authorities", "SUPPORTED", "existing authorities"))
        merged = isc.merge_checks(first, [second])
        self.assertEqual([c.claim for c in merged.claims], ["OCT 2027 target date", "Work runs on existing authorities"])


class PassageTests(unittest.IsolatedAsyncioTestCase):
    def test_paragraphs_keep_the_heading_they_sit_under(self):
        found = [p for p in sp.split(RELEASE) if "8.1 million" in p.text]
        self.assertEqual(found[0].section, "Second Quarter Financial Overview")
        self.assertIn("Second Quarter Financial Overview · paragraph", found[0].label)

    async def test_a_short_source_is_given_whole(self):
        self.assertEqual(await sp.excerpt("Q2 DaaS revenue CAD $8.6M", MEMO), MEMO)

    async def test_a_long_source_is_given_as_the_passages_that_matter(self):
        self.assertGreater(len(RELEASE), sp.MODEL_SOURCE_CHARS)
        shown = await sp.excerpt("CAD $8.6M Q2 DRONE AS A SERVICE REVENUE (DaaS)", RELEASE)
        self.assertLessEqual(len(shown), sp.MODEL_SOURCE_CHARS + 3000)
        self.assertIn("$8.1 million", shown)
        self.assertIn("[Second Quarter Financial Overview · paragraph", shown)

    def test_a_quote_is_not_cut_at_an_abbreviation(self):
        text = "The Pentagon is targeting Oct. 1, 2027, as the new command's start date. Dr. Smith agreed."
        self.assertEqual(sp._SENTENCE.split(text)[0], "The Pentagon is targeting Oct. 1, 2027, as the new command's start date.")

    def test_bylines_and_bullets_are_not_headings(self):
        for line in ("2 minute read", "— Dr. Shaun Passley, ZenaTech Chairman", "• Faster drone delivery",
                     "Test technologies during live exercises and in real-world operational environments"):
            with self.subTest(line=line):
                self.assertFalse(sp._is_heading(line))
        self.assertTrue(sp._is_heading("Project Agincourt lays out the road to AUTOWARCOM"))

    async def test_a_claim_gets_the_sources_own_words_and_place(self):
        claims = [SourceClaim(claim="CAD $8.6M Q2 DaaS revenue", verdict="CONTRADICTED",
                              evidence="The DaaS segment contributed $8.1 million in Q2.", confidence=0.9),
                  SourceClaim(claim="Stacklens ships today", verdict="NOT_FOUND", confidence=0.9)]
        out = await sp.locate(claims, RELEASE)
        self.assertTrue(out[0].source_quote.startswith("The DaaS segment contributed $8.1 million"))
        self.assertIn("Second Quarter Financial Overview", out[0].source_location)
        self.assertIsNone(out[1].source_quote)  # nothing to show for a claim the source doesn't make


if __name__ == "__main__":
    unittest.main()
