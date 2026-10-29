"""Caption HTML sanitizer: keeps the formatting the caption editor can produce and drops everything else.

The editor's HTML is shown back to other people (e.g. an admin opening someone's draft), so it must never carry
scripts, event handlers, styles or javascript: links. Allowlist, not blocklist: anything not listed here is removed,
and the text inside unknown tags is kept as plain text. Standard library only.
"""

from html import escape
from html.parser import HTMLParser
from typing import List, Optional, Tuple

# Tags the caption editor produces (formatBlock, bold/italic/underline, lists, links, line breaks).
ALLOWED_TAGS = {"p", "div", "br", "b", "strong", "i", "em", "u", "h2", "h3", "ul", "ol", "li", "blockquote", "a", "span"}
VOID_TAGS = {"br"}
# Whose content is dropped entirely, not kept as text.
DROP_CONTENT = {"script", "style", "iframe", "object", "embed", "template", "noscript", "svg", "math", "head", "title"}
SAFE_SCHEMES = ("http://", "https://", "mailto:")


def _safe_href(value: Optional[str]) -> Optional[str]:
    if not value:
        return None
    # Browsers ignore control characters and whitespace inside a scheme ("java\tscript:"), so check without them.
    compact = "".join(ch for ch in value.strip() if ch > " " and ch != "\x7f").lower()
    return value.strip() if compact.startswith(SAFE_SCHEMES) else None


class _Sanitizer(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.out: List[str] = []
        self.open: List[str] = []
        self.dropping = 0

    def handle_starttag(self, tag: str, attrs: List[Tuple[str, Optional[str]]]) -> None:
        if tag in DROP_CONTENT:
            self.dropping += 1
            return
        if self.dropping or tag not in ALLOWED_TAGS:
            return
        if tag == "a":
            href = _safe_href(dict(attrs).get("href"))
            self.out.append(f'<a href="{escape(href, quote=True)}" rel="noopener noreferrer" target="_blank">' if href else "<a>")
        else:
            self.out.append(f"<{tag}>")
        if tag not in VOID_TAGS:
            self.open.append(tag)

    def handle_startendtag(self, tag: str, attrs: List[Tuple[str, Optional[str]]]) -> None:
        if tag in VOID_TAGS and not self.dropping:
            self.out.append(f"<{tag}>")

    def handle_endtag(self, tag: str) -> None:
        if tag in DROP_CONTENT:
            self.dropping = max(0, self.dropping - 1)
            return
        if self.dropping or tag not in self.open:
            return
        # Close anything left open inside it, so the output is always well nested.
        while self.open:
            t = self.open.pop()
            self.out.append(f"</{t}>")
            if t == tag:
                break

    def handle_data(self, data: str) -> None:
        if not self.dropping:
            self.out.append(escape(data, quote=False))

    def result(self) -> str:
        self.close()
        while self.open:
            self.out.append(f"</{self.open.pop()}>")
        return "".join(self.out)


def sanitize_caption_html(html: Optional[str]) -> Optional[str]:
    """The caption HTML with only safe formatting left. None stays None."""
    if html is None:
        return None
    parser = _Sanitizer()
    parser.feed(html)
    return parser.result()
