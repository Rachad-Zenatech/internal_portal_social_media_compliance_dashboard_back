"""HTML Layout

Converts Share Document HTML (tables, headings, lists, bold) into document_layout's light-mark plain text
so approved scripts keep their table/heading structure on the script changes and review pages.
"""

from html.parser import HTMLParser
from typing import List, Optional, Tuple

from services.compliance.media.document_layout import _cell, _format_runs, _row


class _HtmlLayoutParser(HTMLParser):
    """Converts rich HTML (tables, headings, lists, bold) into document_layout's light-mark plain text."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.blocks: List[str] = []
        self._runs: List[Tuple[str, bool]] = []
        self._bold = 0
        self._heading: Optional[int] = None
        self._lists: List[Tuple[str, int]] = []
        self._in_cell = False
        self._row_cells: List[str] = []
        self._table_rows: List[str] = []

    def _flush_runs(self) -> str:
        text = _format_runs(self._runs).strip()
        self._runs.clear()
        return text

    def flush_block(self) -> None:
        if self._in_cell:
            return
        text = self._flush_runs()
        if not text:
            return
        if self._heading:
            self.blocks.append(f"{'#' * self._heading} {text}")
        elif self._lists:
            kind, idx = self._lists[-1]
            self.blocks.append((f"{idx}. " if kind == "ol" else "- ") + text)
        else:
            self.blocks.append(text)

    def handle_starttag(self, tag: str, attrs: List[Tuple[str, Optional[str]]]) -> None:
        t = tag.lower()
        if t in ("strong", "b"):
            self._bold += 1
        elif t in ("h1", "h2", "h3"):
            self.flush_block()
            self._heading = int(t[1])
        elif t in ("ul", "ol"):
            self.flush_block()
            self._lists.append((t, 0))
        elif t == "li":
            self.flush_block()
            if self._lists:
                kind, idx = self._lists[-1]
                self._lists[-1] = (kind, idx + 1)
        elif t == "table":
            self.flush_block()
            self._table_rows = []
        elif t == "tr":
            self._row_cells = []
        elif t in ("td", "th"):
            self._in_cell = True
            self._runs.clear()
        elif t == "br":
            self._runs.append((" " if self._in_cell else "\n", False))

    def handle_endtag(self, tag: str) -> None:
        t = tag.lower()
        if t in ("strong", "b"):
            self._bold = max(0, self._bold - 1)
        elif t in ("h1", "h2", "h3"):
            self.flush_block()
            self._heading = None
        elif t in ("p", "div", "li"):
            if self._in_cell:
                self._runs.append((" ", False))
            else:
                self.flush_block()
        elif t in ("ul", "ol"):
            self.flush_block()
            if self._lists:
                self._lists.pop()
        elif t in ("td", "th"):
            self._row_cells.append(_cell(self._flush_runs()))
            self._in_cell = False
        elif t == "tr":
            if any(self._row_cells):
                self._table_rows.append(_row(self._row_cells))
            self._row_cells = []
        elif t == "table":
            if self._table_rows:
                self.blocks.append("\n".join(self._table_rows))
            self._table_rows = []

    def handle_data(self, data: str) -> None:
        if data:
            self._runs.append((data, self._bold > 0 and not self._in_cell and self._heading is None))


def html_to_text(html: Optional[str]) -> str:
    """Turn Share Document HTML into light-mark text so tables, headings, lists and bold keep their layout."""
    if not html or not html.strip():
        return ""
    parser = _HtmlLayoutParser()
    parser.feed(html)
    parser.close()
    parser.flush_block()
    return "\n\n".join(b for b in parser.blocks if b)
