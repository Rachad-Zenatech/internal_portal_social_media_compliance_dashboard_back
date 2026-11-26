"""Download a reviewed script as an editable Word document using its stored layout marks."""
import io
import re
from zipfile import ZipFile, ZIP_DEFLATED
from xml.etree import ElementTree as ET
from services.compliance.media.script_sources import is_sources_label

W = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
ET.register_namespace("w", W)
MIME = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"


def node(parent, tag, **attrs):
    return ET.SubElement(parent, f"{{{W}}}{tag}", {f"{{{W}}}{k}": str(v) for k, v in attrs.items()})


def paragraph(parent, text, heading=0):
    p = node(parent, "p")
    if heading:
        node(node(p, "pPr"), "pStyle", val=f"Heading{heading}")
    for token in re.split(r"(\*\*.*?\*\*|(?<!\w)_[^_]+_(?!\w))", text):
        if not token:
            continue
        bold = token.startswith("**") and token.endswith("**")
        italic = token.startswith("_") and token.endswith("_")
        r = node(p, "r")
        if bold or italic:
            node(node(r, "rPr"), "b" if bold else "i")
        t = node(r, "t")
        t.set("{http://www.w3.org/XML/1998/namespace}space", "preserve")
        t.text = token[2:-2] if bold else token[1:-1] if italic else token
    return p


def script_docx(text):
    root = ET.Element(f"{{{W}}}document")
    body = node(root, "body")
    table = None
    for line in text.splitlines():
        stripped = line.strip()
        if re.fullmatch(r"--- Page \d+ ---", stripped):
            node(node(node(body, "p"), "r"), "br", type="page")
            table = None
        elif stripped.startswith("|") and stripped.endswith("|"):
            cells = [c.strip() for c in stripped[1:-1].split("|")]
            if all(re.fullmatch(r":?-+:?", c) for c in cells):
                continue
            if table is None:
                table = node(body, "tbl")
                props = node(table, "tblPr")
                node(props, "tblW", w=0, type="auto")
                borders = node(props, "tblBorders")
                for edge in ("top", "left", "bottom", "right", "insideH", "insideV"):
                    node(borders, edge, val="single", sz=4, color="CBD5E1")
                grid = node(table, "tblGrid")
                for _ in cells:
                    node(grid, "gridCol", w=9000 // max(1, len(cells)))
            row = node(table, "tr")
            if is_sources_label(next((cell for cell in cells if cell), "")):
                width = max(2, len(table.find(f"{{{W}}}tblGrid")))
                label_span = width // 2
                entries = "; ".join(cells[1:])
                for value, span in ((cells[0], label_span), (entries, width - label_span)):
                    tc = node(row, "tc")
                    node(node(tc, "tcPr"), "gridSpan", val=span)
                    for entry in value.split("; "):
                        paragraph(tc, entry)
            else:
                for cell in cells:
                    paragraph(node(row, "tc"), cell)
        else:
            table = None
            heading = re.match(r"^(#{1,3}) (.*)$", stripped)
            paragraph(body, heading[2] if heading else line, len(heading[1]) if heading else 0)
    section = node(body, "sectPr")
    node(section, "pgSz", w=12240, h=15840)
    node(section, "pgMar", top=1080, right=1080, bottom=1080, left=1080)
    styles = ET.Element(f"{{{W}}}styles")
    for level in (1, 2, 3):
        style = node(styles, "style", type="paragraph", styleId=f"Heading{level}")
        node(style, "name", val=f"heading {level}")
        runs = node(style, "rPr")
        node(runs, "b")
        node(runs, "sz", val=36 - 4 * level)
    content_types = '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types"><Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/><Default Extension="xml" ContentType="application/xml"/><Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/><Override PartName="/word/styles.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.styles+xml"/></Types>'
    relations = '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="word/document.xml"/></Relationships>'
    document_relations = '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/styles" Target="styles.xml"/></Relationships>'
    output = io.BytesIO()
    with ZipFile(output, "w", ZIP_DEFLATED) as archive:
        archive.writestr("[Content_Types].xml", content_types)
        archive.writestr("_rels/.rels", relations)
        archive.writestr("word/_rels/document.xml.rels", document_relations)
        archive.writestr("word/document.xml", ET.tostring(root, encoding="utf-8", xml_declaration=True))
        archive.writestr("word/styles.xml", ET.tostring(styles, encoding="utf-8", xml_declaration=True))
    return output.getvalue()
