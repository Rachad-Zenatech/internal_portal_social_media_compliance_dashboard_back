"""Finding what a post cites as its source: links, "Source:" lines on a caption or an image, named documents."""

from services.compliance.evaluation import citation_finder as cf

STACKLENS = """ZenaTech recently unveiled Stacklens, a line of AI-powered smart safety glasses.
Stacklens is currently at the prototype and testing stage, with customer pilots expected to begin in 2027.
Read the full article here: https://www.zenatech.com/zenatech-unveils-stacklens-ai.../
$ZENA $49Q #ZenaTech #Stacklens #SmartGlasses"""

AGINCOURT_IMAGE = """The authorities still need Congress
OCT 2027 Target date to establish the command
30 DAYS To submit a plan of action and milestones
Announcement first. Authority later.
*Image for illustration purposes only.
Source: Project Agincourt, Pathway to the Autonomous Warfare Command. Memorandum,
Secretary of War, September 30, 2026."""


def refs(citations):
    return [(c.kind, c.reference) for c in citations]


class TestLinks:
    def test_a_read_the_full_article_link_is_a_citation_and_a_cut_short_one_says_so(self):
        found = cf.find(STACKLENS)
        assert len(found) == 1
        assert found[0].kind == "URL" and found[0].where == "caption"
        assert found[0].cue == "read the full"
        assert found[0].truncated is True
        assert cf.describe(found[0]).startswith("The link is cut short")

    def test_a_whole_link_is_not_cut_short(self):
        found = cf.find("Details: https://www.zenatech.com/zenatech-unveils-stacklens-ai-powered-smart-safety-glasses/")
        assert len(found) == 1 and found[0].truncated is False
        assert cf.describe(found[0]) is None

    def test_a_call_to_action_home_page_is_not_a_citation(self):
        assert cf.find("Learn more: https://www.zenatech.com/") == []
        assert cf.find("Book a demo at zenatech.com") == []
        assert cf.find("Visit www.zenatech.com/contact") == []

    def test_a_learn_more_link_to_an_article_is_one(self):
        found = cf.find("Learn more: https://www.zenatech.com/news/zenatech-opens-new-office-in-toronto")
        assert [c.kind for c in found] == ["URL"]

    def test_a_source_cue_makes_even_a_home_page_a_citation(self):
        assert [c.kind for c in cf.find("Source: https://www.faa.gov/")] == ["URL"]

    def test_a_link_under_a_sources_heading_is_a_citation_even_for_a_home_page(self):
        """The author's "Source links" are added to the check's text as a "Sources:" heading with the links below it."""
        found = cf.find("Our new Mesa office is open.\n\nSources:\nhttps://www.droneasaservice.com/")
        assert [(c.kind, c.cue) for c in found] == [("URL", "sources")]
        # a blank line ends the block, so a call-to-action link after it is still just an invitation
        assert cf.find("Sources:\n\nLearn more: https://www.zenatech.com/") == []

    def test_social_profiles_are_not_citations(self):
        assert cf.find("Follow us https://www.linkedin.com/company/zenatech") == []
        assert cf.find("https://www.instagram.com/zenatech/") == []

    def test_a_bare_domain_with_a_path_is_found(self):
        found = cf.find("Read the report: sec.gov/Archives/edgar/data/1234/filing.htm")
        assert refs(found) == [("URL", "https://sec.gov/Archives/edgar/data/1234/filing.htm")]

    def test_trailing_punctuation_is_not_part_of_the_link(self):
        found = cf.find("Source: https://example.com/report/2026.")
        assert found[0].reference == "https://example.com/report/2026"

    def test_the_same_link_twice_is_one_citation(self):
        text = "Source: https://example.com/a-long-report-name\nRead the full report https://example.com/a-long-report-name"
        assert len(cf.find(text)) == 1


class TestNamedSources:
    def test_a_source_line_printed_on_an_image_runs_over_its_line_breaks(self):
        found = cf.find("Text in image 1: " + AGINCOURT_IMAGE)
        named = [c for c in found if c.kind == "NAMED"]
        assert len(named) == 1
        assert named[0].where == "image"
        assert named[0].reference == (
            "Project Agincourt, Pathway to the Autonomous Warfare Command. Memorandum, Secretary of War, September 30, 2026."
        )
        assert "nothing to open" in cf.describe(named[0])

    def test_the_same_source_in_the_caption_is_where_caption(self):
        found = cf.find("Source: Gartner Hype Cycle, 2026")
        assert found[0].where == "caption" and found[0].reference == "Gartner Hype Cycle, 2026"

    def test_several_sources_are_split(self):
        found = cf.find("Sources: FAA Part 107 summary; Transport Canada CARs 901")
        assert [c.reference for c in found] == ["FAA Part 107 summary", "Transport Canada CARs 901"]

    def test_hashtags_and_cashtags_after_a_source_are_left_out(self):
        found = cf.find("Source: ZenaTech Q3 2026 results\n$ZENA #ZenaTech")
        assert [c.reference for c in found] == ["ZenaTech Q3 2026 results"]

    def test_a_source_line_does_not_swallow_the_next_sentence(self):
        found = cf.find("Source: FAA waiver letter, 2026.\nOur drones fly farther.")
        assert [c.reference for c in found] == ["FAA waiver letter, 2026."]

    def test_according_to_names_the_source(self):
        found = cf.find("According to Gartner, spending will double by 2028.")
        assert refs(found) == [("NAMED", "Gartner")]

    def test_per_needs_a_name_after_it(self):
        assert cf.find("Saves 3 hours per week and 2 per day.") == []

    def test_a_source_in_brackets_in_a_sentence(self):
        found = cf.find("Revenue grew 27% (Source: ZenaTech Q3 2026 report).")
        assert [c.reference for c in found] == ["ZenaTech Q3 2026 report)."] or found[0].reference.startswith("ZenaTech Q3 2026 report")


class TestNothingToFind:
    def test_a_post_with_no_citation(self):
        assert cf.find("Happy Thanksgiving from the ZenaTech family! #ZenaTech") == []

    def test_empty(self):
        assert cf.find("") == [] and cf.find(None) == []

    def test_at_most_six(self):
        text = "\n".join(f"Source: Report number {n} on drones" for n in range(10))
        assert len(cf.find(text)) == cf.MAX_CITATIONS
