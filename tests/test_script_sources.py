import io
import unittest
import zipfile
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from models.compliance.compliance_model import SourceCheck
from services.compliance.evaluation import citation_finder, source_check_service
from services.compliance.media import document_layout, html_layout, script_sources

URL = "https://example.com/report"
FOOTER = "| **Sources used** | Market report https://example.com/report; Reuters |"


class ScriptSourceTests(unittest.TestCase):
    def test_merged_row_is_reference_material_and_not_script_wording(self):
        wording, sources = script_sources.split("Eight projects.\n" + FOOTER)
        self.assertEqual(wording, "Eight projects.")
        found = citation_finder.find(sources, explicit_script=True)
        self.assertEqual([(citation.kind, citation.reference) for citation in found],
                         [("NAMED", "Reuters"), ("URL", URL)])

    def test_optional_template_placeholder_is_ignored(self):
        wording, sources = script_sources.split(
            "Eight projects.\n| Sources used | [List the source names and links used to create this script.] |"
        )
        self.assertEqual((wording, sources), ("Eight projects.", ""))

    def test_editor_preserves_clickable_source(self):
        text = html_layout.html_to_text(
            f'<table><tr><td>Sources used</td><td><a href="{URL}">Market report</a></td></tr></table>'
        )
        self.assertEqual(script_sources.split(text)[1], f"Sources:\n{URL}")

    def test_word_preserves_clickable_source_in_merged_row(self):
        data = io.BytesIO()
        with zipfile.ZipFile(data, "w") as file:
            file.writestr("word/document.xml", """<w:document
                xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"
                xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"><w:body><w:tbl><w:tr>
                <w:tc><w:tcPr><w:gridSpan w:val="2"/></w:tcPr><w:p><w:r><w:t>Sources used</w:t></w:r></w:p></w:tc>
                <w:tc><w:tcPr><w:gridSpan w:val="2"/></w:tcPr><w:p><w:hyperlink r:id="rId1">
                <w:r><w:t>Market report</w:t></w:r></w:hyperlink></w:p></w:tc>
                </w:tr></w:tbl></w:body></w:document>""")
            file.writestr("word/_rels/document.xml.rels", f"""<Relationships
                xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
                <Relationship Id="rId1" TargetMode="External" Target="{URL}"/>
                </Relationships>""")
        text = document_layout.docx_to_text(data.getvalue())
        self.assertEqual(script_sources.split(text)[1], f"Sources:\n{URL}")


class ScriptSourceIntegration(unittest.IsolatedAsyncioTestCase):
    async def test_script_links_and_newsroom_use_the_script_claims(self):
        newsroom = AsyncMock(return_value=None)
        source = SourceCheck(reference=URL, kind="URL", where="script", status="READ")
        read = AsyncMock(return_value=(source, None))
        with patch.object(source_check_service, "ENABLED", True), \
             patch.object(source_check_service.newsroom_facts, "material", newsroom), \
             patch.object(source_check_service, "_read_link", read):
            await source_check_service.run("Eight projects.", source_script=FOOTER)
        newsroom.assert_awaited_once_with("Eight projects.")
        citation, wording = read.await_args.args
        self.assertEqual((citation.reference, citation.where, wording), (URL, "script", "Eight projects."))

    async def test_script_check_sends_footer_separately(self):
        from services.compliance.evaluation import script_review_service as scripts

        with patch.object(scripts.subs, "check_request") as request, \
             patch("services.compliance.evaluation.script_compliance_check.check_compliance", AsyncMock(return_value=SimpleNamespace(model_dump=lambda **_: {}, ai_unavailable=False))):
            await scripts.check_text({"title": "Project update"}, "Eight projects.\n" + FOOTER)
        self.assertEqual(request.call_args.args[1], "Project update\n\nEight projects.")
        self.assertIn(URL, request.call_args.kwargs["source_script"])
