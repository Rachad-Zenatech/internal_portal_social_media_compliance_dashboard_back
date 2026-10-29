import io
import tempfile
import unittest
import zipfile
from pathlib import Path

from services import document_text_service as dts

W = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"


def docx(*paragraphs: str) -> bytes:
    """A minimal .docx with these paragraphs ("\\t" and "\\n" become a tab and a line break)."""
    def run(text: str) -> str:
        out = []
        for i, line in enumerate(text.split("\n")):
            if i:
                out.append("<w:br/>")
            for j, part in enumerate(line.split("\t")):
                if j:
                    out.append("<w:tab/>")
                out.append(f'<w:t xml:space="preserve">{part}</w:t>')
        return f"<w:r>{''.join(out)}</w:r>"

    body = "".join(f"<w:p>{run(p)}</w:p>" for p in paragraphs)
    xml = f'<?xml version="1.0"?><w:document xmlns:w="{W}"><w:body>{body}</w:body></w:document>'
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("word/document.xml", xml)
    return buf.getvalue()


class ReadTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)

    def write(self, name: str, data: bytes) -> Path:
        path = Path(self.dir.name) / name
        path.write_bytes(data)
        return path

    def test_docx_paragraphs_are_read_in_order(self):
        path = self.write("script.docx", docx("SCENE 1", "Shaun: Our drone is combat proven.", "", "Cut to\tlogo"))
        self.assertEqual(dts.read_local(path), "SCENE 1\nShaun: Our drone is combat proven.\nCut to logo")

    def test_a_line_break_inside_a_paragraph_starts_a_new_line(self):
        path = self.write("script.docx", docx("First line\nsecond line"))
        self.assertEqual(dts.read_local(path), "First line\nsecond line")

    def test_text_files_in_common_encodings(self):
        self.assertEqual(dts.read_local(self.write("a.txt", "Café launch\r\n\r\nDay two".encode("utf-8"))), "Café launch\nDay two")
        self.assertEqual(dts.read_local(self.write("b.md", "Café".encode("cp1252"))), "Café")
        self.assertEqual(dts.read_local(self.write("c.txt", "Café".encode("utf-16"))), "Café")

    def test_old_word_files_and_broken_docx_say_what_to_do(self):
        with self.assertRaisesRegex(dts.UnreadableDocument, r"\.docx or PDF"):
            dts.read_local(self.write("old.doc", b"\xd0\xcf\x11\xe0"))
        with self.assertRaisesRegex(dts.UnreadableDocument, "valid Word document"):
            dts.read_local(self.write("broken.docx", b"not a zip"))

    def test_pdfs_are_left_to_the_model(self):
        self.assertIsNone(dts.read_local(self.write("script.pdf", b"%PDF-1.7")))

    def test_long_text_is_capped(self):
        text = dts.read_local(self.write("long.txt", ("word " * 20000).encode()))
        self.assertLessEqual(len(text), dts.MAX_TEXT_CHARS)


class WhichFilesTests(unittest.TestCase):
    def test_scripts_and_article_files_are_read(self):
        self.assertTrue(dts.is_document_file("SCRIPT", "Thanksgiving script.docx"))
        self.assertTrue(dts.is_document_file("MAIN", "article.PDF"))
        self.assertFalse(dts.is_document_file("SUPPORTING", "brief.docx"))
        self.assertFalse(dts.is_document_file("MAIN", "video.mp4"))


if __name__ == "__main__":
    unittest.main()
