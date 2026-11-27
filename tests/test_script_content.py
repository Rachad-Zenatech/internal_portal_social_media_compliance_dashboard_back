import pytest

from services.compliance.media.script_content import sections, published


@pytest.mark.parametrize("voice,screen", [("Voiceover", "Supers"), ("VO", "On-screen text"), ("Narration", "Text overlay")])
def test_table_published_copy_is_only_voiceover_and_supers(voice, screen):
    text = f"Storyline: Internal angle\n| Timecode | Visuals | {voice} | {screen} |\n| 0:00 | Show a uniform | A factual claim | HELLO |\nEdit instructions: Replace the stock clip"
    parts = sections(text, "Internal title")
    assert published(parts) == "A factual claim\nHELLO"
    assert parts["visuals"] == "Show a uniform"
    assert parts["timing"] == "0:00"
    assert "Internal angle" in parts["details"]
    assert "Internal title" in parts["details"]
    assert "Replace the stock clip" in parts["details"]


def test_reordered_columns_and_repeated_page_headers_keep_their_meaning():
    text = "| Supers | Voiceover | Time | Visual / footage |\n| FIRST | First sentence | 0:00 | First visual |\n--- Page 2 ---\n| Supers | Voiceover | Time | Visual / footage |\n| SECOND | Second sentence | 0:05 | Second visual |"
    parts = sections(text)
    assert parts["voiceover"] == "First sentence\nSecond sentence"
    assert parts["supers"] == "FIRST\nSECOND"
    assert "visual" not in published(parts)


def test_explicit_sections_support_multiline_copy_and_stop_at_other_headings():
    parts = sections("# Voiceover\nFirst sentence\nSecond sentence\nSupers: READ THIS\n# Production plan\nInternal instructions\nVisuals: Show the aircraft")
    assert published(parts) == "First sentence\nSecond sentence\nREAD THIS"
    assert "Internal instructions" in parts["details"]
    assert parts["visuals"] == "Show the aircraft"


def test_unlabelled_text_is_never_assumed_to_be_published():
    parts = sections("Some text in an unknown script format")
    assert published(parts) == ""
    assert "Some text" in parts["details"]


def test_escaped_pipes_do_not_shift_columns():
    parts = sections(r"| Time | Visuals | Voiceover | Supers |" + "\n" + r"| 0:00 | A \| B | Spoken words | TEXT |")
    assert parts["visuals"] == "A | B"
    assert published(parts) == "Spoken words\nTEXT"


def test_a_data_cell_named_voiceover_is_not_mistaken_for_a_header():
    parts = sections("| Time | Visuals | Voiceover | Supers |\n| 0:00 | Camera | A sentence | Voiceover |")
    assert parts["voiceover"] == "A sentence"
    assert parts["supers"] == "Voiceover"
