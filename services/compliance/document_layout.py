"""Document Layout
Turns a script's .docx or PDF into text that keeps its structure, so it reads like the original when it's shown.
The text stays plain (it's what the rulebook check, the sentiment analysis and the reviewer's edits work on), with
four light conventions the page understands:

    --- Page 2 ---        a new page
    # Title               a heading
    | a | b | c |         one row of a table (the first row of a run of rows is its header)
    **bold**              bold wording

plain_text() takes those marks back out, for anything that reads the wording rather than shows it.
"""

import io
import logging
import re
import zipfile
from collections import Counter
from typing import Any, Dict, List, Optional, Tuple
from xml.etree import ElementTree

logger = logging.getLogger(__name__)

_W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
PAGE_RE = re.compile(r"^--- Page (\d+) ---$")
# A paragraph this big (half-points in Word) reads as a heading even without a heading style.
DOCX_HEADING_HALF_POINTS = 32
# A PDF line this much bigger than the body text is a heading; bold at a smaller step counts too.
PDF_HEADING_RATIO = 1.3
PDF_BOLD_HEADING_RATIO = 1.12
# Fewer characters than this and a PDF is a scan (or empty), so it goes to the AI reader instead.
MIN_PDF_CHARS = 40


def page_marker(number: int) -> str:
    return f"--- Page {number} ---"


def _format_runs(runs: List[Tuple[str, bool]]) -> str:
    """Runs of (text, bold) as one line, each bold stretch wrapped in ** (spacing kept outside the marks)."""
    merged: List[List[Any]] = []
    for text, bold in runs:
        if not text:
            continue
        if merged and merged[-1][1] == bold:
            merged[-1][0] += text
        else:
            merged.append([text, bold])
    out = []
    for text, bold in merged:
        if bold and text.strip():
            out.append(f"{text[:len(text) - len(text.lstrip())]}**{text.strip()}**{text[len(text.rstrip()):]}")
        else:
            out.append(text)
    return "".join(out)


def _cell(text: str) -> str:
    return " ".join(text.replace("|", "/").split())


def _row(cells: List[str]) -> str:
    return "| " + " | ".join(cells) + " |"


# --- Word -------------------------------------------------------------------------------------------------------------

def _is_on(el: Optional[ElementTree.Element]) -> bool:
    """A toggle property such as <w:b/>: on unless it says val="0" or "false"."""
    if el is None:
        return False
    return el.get(f"{_W}val", "true").lower() not in ("0", "false", "off")


def _style_names(zf: zipfile.ZipFile) -> Dict[str, str]:
    try:
        root = ElementTree.fromstring(zf.read("word/styles.xml"))
    except (KeyError, ElementTree.ParseError):
        return {}
    names = {}
    for style in root.iter(f"{_W}style"):
        name = style.find(f"{_W}name")
        if name is not None:
            names[style.get(f"{_W}styleId", "")] = (name.get(f"{_W}val") or "").lower()
    return names


def _is_heading_style(style_id: str, names: Dict[str, str]) -> bool:
    name = names.get(style_id, style_id).lower()
    return name.startswith("heading") or name in ("title", "subtitle")


class _Docx:
    def __init__(self, zf: zipfile.ZipFile):
        self.styles = _style_names(zf)
        self.lines: List[str] = []
        self.page = 1

    def _runs(self, p: ElementTree.Element) -> Tuple[List[Tuple[str, bool]], List[int], bool]:
        """A paragraph's (text, bold) runs, the positions in them where a page break falls, and whether every word
        is set large."""
        runs: List[Tuple[str, bool]] = []
        breaks: List[int] = []
        sizes: List[int] = []
        for r in p.iter(f"{_W}r"):
            props = r.find(f"{_W}rPr")
            bold = props is not None and _is_on(props.find(f"{_W}b"))
            size = props.find(f"{_W}sz") if props is not None else None
            text = []
            for el in r:
                if el.tag == f"{_W}t" and el.text:
                    text.append(el.text)
                elif el.tag == f"{_W}tab":
                    text.append(" ")
                elif el.tag in (f"{_W}br", f"{_W}cr"):
                    if el.get(f"{_W}type") == "page":
                        if text:
                            runs.append(("".join(text), bold))
                            text = []
                        breaks.append(len(runs))
                    else:
                        text.append("\n")
            joined = "".join(text)
            if joined:
                runs.append((joined, bold))
                if joined.strip() and size is not None:
                    sizes.append(int(size.get(f"{_W}val", "0") or 0))
                elif joined.strip():
                    sizes.append(0)
        return runs, breaks, bool(sizes) and min(sizes) >= DOCX_HEADING_HALF_POINTS

    def paragraph(self, p: ElementTree.Element, in_table: bool = False) -> str:
        props = p.find(f"{_W}pPr")
        style = props.find(f"{_W}pStyle") if props is not None else None
        heading = style is not None and _is_heading_style(style.get(f"{_W}val", ""), self.styles)
        if props is not None and props.find(f"{_W}pageBreakBefore") is not None and not in_table:
            self.break_page()
        runs, breaks, large = self._runs(p)
        if in_table:
            return _cell(_format_runs(runs))
        start = 0
        for at in breaks:
            self.emit(_format_runs(runs[start:at]), heading or large)
            self.break_page()
            start = at
        return self.emit(_format_runs(runs[start:]), heading or large)

    def emit(self, text: str, heading: bool) -> str:
        for line in text.split("\n"):
            if heading and line.strip():
                self.lines.append("# " + line.replace("**", "").strip())
            else:
                self.lines.append(line)
        return text

    def break_page(self) -> None:
        if self.page == 1:
            # Only a document with more than one page is marked into pages.
            self.lines.insert(0, page_marker(1))
        self.page += 1
        self.lines.append(page_marker(self.page))

    def table(self, tbl: ElementTree.Element) -> None:
        for tr in tbl.findall(f"{_W}tr"):
            cells = []
            for tc in tr.findall(f"{_W}tc"):
                cells.append(" ".join(c for c in (self.paragraph(p, in_table=True) for p in tc.iter(f"{_W}p")) if c))
            if any(cells):
                self.lines.append(_row(cells))

    def block(self, node: ElementTree.Element) -> None:
        for child in node:
            if child.tag == f"{_W}p":
                self.paragraph(child)
            elif child.tag == f"{_W}tbl":
                self.table(child)
            elif child.tag in (f"{_W}sdt", f"{_W}sdtContent"):
                self.block(child)


def docx_to_text(data: bytes) -> str:
    """A .docx with its headings, bold wording, tables and page breaks marked (see the module's notes)."""
    with zipfile.ZipFile(io.BytesIO(data)) as zf:
        root = ElementTree.fromstring(zf.read("word/document.xml"))
        doc = _Docx(zf)
    body = root.find(f"{_W}body")
    if body is not None:
        doc.block(body)
    return "\n".join(doc.lines)


# --- PDF --------------------------------------------------------------------------------------------------------------

def _span_bold(span: Dict[str, Any]) -> bool:
    return bool(span.get("flags", 0) & 16) or "bold" in (span.get("font") or "").lower()


def _body_size(pages: List[Dict[str, Any]]) -> float:
    """The most common text size, by characters: what the body is set in."""
    sizes: Counter = Counter()
    for page in pages:
        for block in page["blocks"]:
            for line in block.get("lines", []):
                for span in line["spans"]:
                    sizes[round(span["size"], 1)] += len(span["text"].strip())
    return sizes.most_common(1)[0][0] if sizes else 10.0


def _centre_in(bbox: Tuple[float, float, float, float], cell: Tuple[float, float, float, float]) -> bool:
    cx, cy = (bbox[0] + bbox[2]) / 2, (bbox[1] + bbox[3]) / 2
    return cell[0] <= cx <= cell[2] and cell[1] <= cy <= cell[3]


def _inside(bbox: Tuple[float, float, float, float], tables: List[Tuple[float, float, float, float]]) -> bool:
    return any(_centre_in(bbox, t) for t in tables)


def _cell_text(page: Any, spans: List[Dict[str, Any]], bbox: Tuple[float, float, float, float]) -> str:
    """The words inside a box, read from the page (so the spacing comes out right); all bold wraps them in **."""
    import pymupdf

    text = _cell(page.get_text("text", clip=pymupdf.Rect(bbox)) or "")
    if text:
        inside = [s for s in spans if s["text"].strip() and _centre_in(s["bbox"], bbox)]
        if inside and all(_span_bold(s) for s in inside):
            return f"**{text}**"
    return text


def _table_rows(page: Any, table: Any, spans: List[Dict[str, Any]]) -> List[str]:
    """A table as marked rows. The columns are the header row's; a row starts wherever the first column has text
    (find_tables sometimes joins two rows or splits one, so its own rows aren't trusted), and each cell is the words
    inside that row and column."""
    header = [c for c in table.rows[0].cells if c is not None] if table.rows else []
    if len(header) < 2:
        return []
    cols = [(c[0], c[2]) for c in header]
    top, bottom = header[0][3], table.bbox[3]
    starts = sorted({round(w[1], 1) for w in page.get_text("words", clip=(cols[0][0], top, cols[0][1], bottom))})
    # Lines of one wrapped first-column cell sit close together; a new row starts after a real gap.
    firsts: List[float] = []
    for y in starts:
        if not firsts or y - firsts[-1] > 8:
            firsts.append(y)
    edges = [top, *[y - 2 for y in firsts], bottom + 1]
    rows = [[_cell_text(page, spans, (x0, header[0][1], x1, top)) for x0, x1 in cols]]
    for y0, y1 in zip(edges[1:], edges[2:]):
        rows.append([_cell_text(page, spans, (x0, y0, x1, y1)) for x0, x1 in cols])
    return [_row(r) for r in rows if any(r)]


def _table_rows_by_cells(page: Any, table: Any, spans: List[Dict[str, Any]]) -> List[str]:
    """The fallback for tables without a clear first column: find_tables' own rows, empty columns dropped and a row
    with an empty first cell joined to the one above it."""
    grid = []
    for cells in table.rows:
        grid.append([_cell_text(page, spans, c) if c is not None else "" for c in cells.cells])
    keep = [c for c in range(max((len(r) for r in grid), default=0)) if any(c < len(r) and r[c] for r in grid)]
    rows = [[r[c] if c < len(r) else "" for c in keep] for r in grid]
    merged: List[List[str]] = []
    for row in rows:
        if not any(row):
            continue
        if merged and not row[0]:
            merged[-1] = [_join_cells(a, b) for a, b in zip(merged[-1], row)]
        else:
            merged.append(row)
    return [_row(r) for r in merged]


def _join_cells(a: str, b: str) -> str:
    """Two halves of one cell. Bold marks that end one half and start the other are joined into one stretch."""
    if not b:
        return a
    if not a:
        return b
    if a.endswith("**") and b.startswith("**"):
        return f"{a[:-2]} {b[2:]}"
    return f"{a} {b}"


def pdf_to_text(data: bytes) -> Optional[str]:
    """A digital PDF with its headings, bold wording, tables and pages marked, or None when it has no text layer
    (a scan) or can't be read here, so the AI reader takes it."""
    try:
        import pymupdf
    except ImportError:
        logger.info("PyMuPDF isn't installed; PDFs are read by the AI reader")
        return None
    try:
        pdf = pymupdf.open(stream=data, filetype="pdf")
    except Exception as e:
        logger.info("PyMuPDF couldn't open the PDF (%s)", e)
        return None
    with pdf:
        layouts = [page.get_text("dict") for page in pdf]
        body = _body_size(layouts)
        repeated = _repeated_lines(layouts)
        lines: List[str] = []
        for number, (page, layout) in enumerate(zip(pdf, layouts), start=1):
            if len(pdf) > 1:
                lines.append(page_marker(number))
            try:
                tables = list(page.find_tables().tables)
            except Exception:
                tables = []
            boxes = [tuple(t.bbox) for t in tables]
            spans = [s for b in layout["blocks"] if b.get("type") == 0 for ln in b["lines"] for s in ln["spans"]]
            items: List[Tuple[float, List[str]]] = [(t.bbox[1], _table_rows(page, t, spans) or _table_rows_by_cells(page, t, spans)) for t in tables]
            for block in layout["blocks"]:
                if block.get("type") != 0 or _inside(tuple(block["bbox"]), boxes):
                    continue
                items.append((block["bbox"][1], _block_lines(block, body, repeated)))
            for _, rows in sorted(items, key=lambda item: item[0]):
                lines.extend(rows)
    text = "\n".join(lines)
    return text if len(PAGE_RE.sub("", text).strip()) >= MIN_PDF_CHARS else None


def _line_key(text: str) -> str:
    """A line with its digits blanked, so "Page 3" and "Page 4" read as the same running header."""
    return re.sub(r"\d+", "#", " ".join(text.split())).lower()


def _line_text(line: Dict[str, Any]) -> str:
    return "".join(s["text"] for s in line["spans"]).strip()


def _repeated_lines(layouts: List[Dict[str, Any]]) -> set:
    """Lines that head or foot most pages (a running title, "Page 3"): shown as page breaks already, so left out."""
    if len(layouts) < 3:
        return set()
    seen: Counter = Counter()
    for layout in layouts:
        keys = {_line_key(_line_text(ln)) for b in layout["blocks"] if b.get("type") == 0 for ln in b["lines"]}
        seen.update(k for k in keys if k)
    return {k for k, n in seen.items() if n >= max(3, len(layouts) * 0.6)}


def _style(line: Dict[str, Any]) -> Tuple[int, int]:
    """The size and colour of a line's last words, which is what its text is set in after any bold label."""
    spans = [s for s in line["spans"] if s["text"].strip()]
    last = spans[-1] if spans else line["spans"][-1]
    return round(last["size"]), last.get("color", 0)


def _block_lines(block: Dict[str, Any], body: float, repeated: set) -> List[str]:
    """A text block as paragraphs: its visual lines joined, except where the style changes or a short line ends one.
    A line set larger than the body is a heading."""
    lines = [ln for ln in block["lines"] if _line_text(ln) and _line_key(_line_text(ln)) not in repeated]
    widest = max((ln["bbox"][2] - ln["bbox"][0] for ln in lines), default=0)
    groups: List[List[Dict[str, Any]]] = []
    for ln in lines:
        if not groups:
            groups.append([])
        else:
            prev = groups[-1][-1]
            short = (prev["bbox"][2] - prev["bbox"][0]) < widest * 0.7
            if _style(prev) != _style(ln) or short:
                groups.append([])
        groups[-1].append(ln)
    out: List[str] = []
    for group in groups:
        runs: List[Tuple[str, bool]] = []
        biggest = 0.0
        all_bold = True
        for ln in group:
            for span in ln["spans"]:
                if not span["text"].strip():
                    runs.append((span["text"], False))
                    continue
                runs.append((span["text"], _span_bold(span)))
                biggest = max(biggest, span["size"])
                all_bold = all_bold and _span_bold(span)
            runs.append((" ", False))
        text = " ".join(_format_runs(runs).split())
        if not text:
            continue
        heading = biggest >= body * PDF_HEADING_RATIO or (all_bold and biggest >= body * PDF_BOLD_HEADING_RATIO)
        out.append("# " + text.replace("**", "") if heading and len(text) <= 160 else text)
    return out


# --- Back to plain wording --------------------------------------------------------------------------------------------

def plain_text(text: Optional[str]) -> str:
    """The wording alone: page marks dropped, headings and bold unmarked, table cells run on."""
    out: List[str] = []
    for line in (text or "").splitlines():
        s = line.strip()
        if PAGE_RE.match(s):
            continue
        s = re.sub(r"^#{1,3}\s+", "", s)
        if s.startswith("|") and s.endswith("|") and len(s) > 1:
            s = "  ".join(c.strip() for c in s.strip("|").split("|"))
        out.append(s.replace("**", ""))
    return "\n".join(out)
