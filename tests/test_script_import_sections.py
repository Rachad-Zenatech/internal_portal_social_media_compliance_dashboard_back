"""Collection titles are metadata; script markers start the imported pages."""
from services.compliance.evaluation.script_import_sections import script_import_sections
from services.compliance.evaluation.script_import_format import script_import_warnings
from services.compliance.evaluation.script_split import split_scripts


TABLE = "| Time | Visual / footage | Voiceover | On-screen text |\n| 0:00 | Camera | Hello world | Hello |"
BODY = f"Storyline: An example\nHook score 80 | about 30 seconds | Green lane\n{TABLE}\nSources used: Editorial commentary"


def test_document_title_and_subtitle_are_not_an_extra_script():
    text = f"--- Page 1 ---\n# Collection title\nTen scripts for review\n# 1 First script\n{BODY}\n# 2 Second script\n{BODY}"
    title, _ = script_import_sections(text)
    scripts = split_scripts(text)
    assert title == "Collection title"
    assert [title for title, _ in scripts] == ["First script", "Second script"]
    assert all("Ten scripts for review" not in body for _, body in scripts)


def test_titleless_numbered_scripts_keep_their_original_partitioning():
    scripts = split_scripts(f"# 1 First script\n{BODY}\n# 2 Second script\n{BODY}")
    assert [title for title, _ in scripts] == ["First script", "Second script"]


def test_plain_numbered_script_names_allow_a_document_title():
    title, text = script_import_sections(f"Collection\n1. First script\n{BODY}\n2. Second script\n{BODY}")
    assert title == "Collection"
    assert [title for title, _ in split_scripts(text)] == ["First script", "Second script"]


def test_bulleted_script_names_with_tables_allow_a_document_title():
    text = f"# Collection\n- First script\n{BODY}\n\u2022 Second script\n{BODY}"
    assert script_import_sections(text)[0] == "Collection"
    assert [title for title, _ in split_scripts(text)] == ["First script", "Second script"]


def test_hierarchical_headings_preserve_the_collection_name():
    text = f"# Collection\n## First script\n{BODY}\n## Second script\n{BODY}"
    assert script_import_sections(text)[0] == "Collection"
    assert [title for title, _ in split_scripts(text)] == ["First script", "Second script"]


def test_numbered_sources_are_not_scripts():
    text = f"A single script\n{BODY}\n1. Source one https://example.com\n2. Source two https://example.org"
    assert len(split_scripts(text)) == 1


def test_plain_bullets_in_a_single_script_are_not_script_boundaries():
    text = "Single script\n- Introduce the aircraft\n- Show the camera\n- End with the logo"
    assert script_import_sections(text) == (None, text)


def test_numbered_sources_do_not_override_numbered_script_headings():
    sources = "\n1. Source one https://example.com\n2. Source two https://example.org"
    text = f"# Collection\n# 1 First script\n{BODY}{sources}\n# 2 Second script\n{BODY}{sources}"
    assert [title for title, _ in split_scripts(text)] == ["First script", "Second script"]


def test_timestamps_do_not_override_script_numbers():
    text = f"Collection\n1. First script\n0:00 Opening\n{BODY}\n2. Second script\n0:00 Opening\n{BODY}"
    assert script_import_sections(text)[0] == "Collection"
    assert len(split_scripts(text)) == 2


def test_word_headings_without_visible_list_numbers_skip_the_document_title():
    text = f"--- Page 1 ---\n# Defense Voiceover Scripts Set 5\nCollection subtitle\n# Start with the decision\n{BODY}\n# Everything on the scale\n{BODY}"
    assert script_import_sections(text)[0] == "Defense Voiceover Scripts Set 5"
    assert [title for title, _ in split_scripts(text)] == ["Start with the decision", "Everything on the scale"]


def test_source_subheadings_stay_with_their_word_script():
    text = f"# Collection\n# First script\n{BODY}\n# Sources and factual basis\nFirst reference\n# Second script\n{BODY}\n# Sources and factual basis\nSecond reference"
    scripts = split_scripts(text)
    assert [title for title, _ in scripts] == ["First script", "Second script"]
    assert "First reference" in scripts[0][1]
    assert "Second reference" not in scripts[0][1]
    assert "Second reference" in scripts[1][1]


def test_titleless_word_scripts_keep_sources_without_extra_partitions():
    text = f"# First script\n{BODY}\n# Sources and factual basis\nFirst reference\n# Second script\n{BODY}"
    assert script_import_sections(text)[0] is None
    assert [title for title, _ in split_scripts(text)] == ["First script", "Second script"]


def test_export_template_structure_has_no_warning():
    assert script_import_warnings([("Example", BODY)]) == []


def test_alternate_table_headers_and_missing_sections_warn_without_blocking():
    other = "| Timecode | Visuals | Voiceover | Supers |\n| 0:00 | Camera | Hello | Hello |"
    scripts = split_scripts(f"# 1 First\n{other}\n# 2 Second\n{BODY}")
    assert len(scripts) == 2
    assert "does not match the export template format" in script_import_warnings(scripts)[0]
