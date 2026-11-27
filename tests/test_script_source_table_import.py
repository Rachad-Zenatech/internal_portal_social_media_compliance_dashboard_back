"""Sources remain a merged footer row through Word import, bulk splitting and HTML reads."""
import io
import zipfile
from xml.etree import ElementTree as ET

from services.compliance.evaluation.script_doc_templates import text_to_html
from services.compliance.evaluation.script_split import split_scripts
from services.compliance.media.document_layout import docx_to_text
from services.compliance.media.html_layout import html_to_text
from services.compliance.media.script_sources import split


def word_document():
    table = '''<w:tbl>
      <w:tr><w:tc><w:p><w:r><w:t>Timecode</w:t></w:r></w:p></w:tc>
      <w:tc><w:p><w:r><w:t>Visuals</w:t></w:r></w:p></w:tc>
      <w:tc><w:p><w:r><w:t>Voiceover</w:t></w:r></w:p></w:tc>
      <w:tc><w:p><w:r><w:t>Supers</w:t></w:r></w:p></w:tc></w:tr>
      <w:tr><w:tc><w:p><w:r><w:t>0:33</w:t></w:r></w:p></w:tc>
      <w:tc><w:p><w:r><w:t>Closed case on the scale</w:t></w:r></w:p></w:tc>
      <w:tc><w:p><w:r><w:t>Weigh the delivery</w:t></w:r></w:p></w:tc>
      <w:tc><w:p><w:r><w:t>WEIGH THE DELIVERY</w:t></w:r></w:p></w:tc></w:tr>
      <w:tr><w:tc><w:tcPr><w:gridSpan w:val="2"/></w:tcPr>
      <w:p><w:r><w:t>Sources used</w:t></w:r></w:p></w:tc>
      <w:tc><w:tcPr><w:gridSpan w:val="2"/></w:tcPr>
      <w:p><w:r><w:t>1. Intended defense roles </w:t></w:r>
      <w:hyperlink r:id="rId1"><w:r><w:t>Military applications</w:t></w:r></w:hyperlink></w:p>
      <w:p><w:r><w:t>2. Development status </w:t></w:r>
      <w:hyperlink r:id="rId2"><w:r><w:t>Company announcement</w:t></w:r></w:hyperlink></w:p>
      <w:p><w:r><w:t>Opening and closing lines are commentary.</w:t></w:r></w:p>
      </w:tc></w:tr></w:tbl>'''
    xml = f'''<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"
      xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"><w:body>{table}</w:body></w:document>'''
    rels = '''<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
      <Relationship Id="rId1" TargetMode="External" Target="https://example.com/military"/>
      <Relationship Id="rId2" TargetMode="External" Target="https://example.com/development"/>
      </Relationships>'''
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("word/document.xml", xml)
        archive.writestr("word/_rels/document.xml.rels", rels)
    return buffer.getvalue()


def test_word_sources_keep_names_links_and_notes_in_two_cells():
    text = docx_to_text(word_document())
    source_line = next(line for line in text.splitlines() if "Sources used" in line)
    assert len(source_line.strip("| ").split("|")) == 2
    assert "Military applications https://example.com/military; 2. Development" in source_line
    assert "Company announcement https://example.com/development; Opening" in source_line
    root = ET.fromstring(f"<root>{text_to_html('Scale', text)}</root>")
    rows = root.findall(".//tr")
    assert [len(row) for row in rows] == [4, 4, 2]
    assert [cell.get("colspan") for cell in rows[-1]] == ["2", "2"]
    assert len(rows[-1][1].findall("p")) == 3


def test_bulk_splitting_keeps_numbered_sources_with_their_script():
    first = docx_to_text(word_document())
    text = f"Script 1: Scale\n{first}\nScript 2: Different topic\n| Time | Visual | Voice | Text |\n| 0:00 | Camera | Hello | Hello |\n| Sources used | Other https://example.com/other |"
    scripts = split_scripts(text)
    assert len(scripts) == 2
    assert "https://example.com/military" in scripts[0][1]
    assert "https://example.com/other" not in scripts[0][1]
    assert "https://example.com/military" not in scripts[1][1]
    body, sources = split(scripts[0][1])
    assert "Sources used" not in body
    assert "https://example.com/military" in sources
    assert "https://example.com/development" in sources


def test_legacy_source_columns_do_not_expand_the_beat_grid():
    html = text_to_html("Scale", "| Time | Visual | Voice | Text |\n| Sources used | Notes | https://example.com/one | https://example.com/two | https://example.com/three |")
    rows = ET.fromstring(f"<root>{html}</root>").findall(".//tr")
    assert [len(row) for row in rows] == [4, 2]
    assert [cell.get("colspan") for cell in rows[-1]] == ["2", "2"]


def test_html_links_stay_inside_sources_cell_across_repeated_reads():
    html = '<table><tr><th>Time</th><th>Visual</th><th>Voice</th><th>Text</th></tr><tr><td colspan="2">Sources used</td><td colspan="2"><p><a href="https://example.com/military">Military applications</a></p></td></tr></table>'
    for _ in range(3):
        text = html_to_text(html)
        source_line = next(line for line in text.splitlines() if "Sources used" in line)
        assert len(source_line.strip("| ").split("|")) == 2
        assert source_line.count("https://example.com/military") == 1
        html = text_to_html("Scale", text)
