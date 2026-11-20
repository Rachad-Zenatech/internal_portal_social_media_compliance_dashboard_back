"""Company names and the company's exchange/ticker label are identification, not claims."""
import re

from .image_marks import IMAGE_MARK

VERSION = "branding-1"
_NAMES = {
    name + suffix
    for name in ("zenatech", "zenadrone", "zena", "zenastudios", "zooffice")
    for suffix in ("", "inc", "incorporated", "corp", "corporation", "ltd", "limited")
}
_LISTINGS = {"nasdaqzena", "nasdaqtickerzena", "nasdaqsymbolzena", "tickerzena"}
_IDENTIFIERS = _NAMES | _LISTINGS | {name + listing for name in _NAMES for listing in _LISTINGS}
_LISTING_STATEMENT = re.compile(
    r"zenatech(?:inc|incorporated)?(?:is)?(?:listed|traded)on(?:the)?nasdaq"
    r"(?:as|under(?:the)?(?:ticker|symbol))?zena"
)


def is_branding(text):
    """Match identification alone; statements about revenue, products or performance remain claims."""
    key = re.sub(r"[^a-z0-9]", "", IMAGE_MARK.sub("", text or "").casefold())
    return key in _IDENTIFIERS or bool(_LISTING_STATEMENT.fullmatch(key))


def without_branding(text):
    """Remove standalone branding lines while preserving image boundaries and substantive sentences."""
    text = re.sub(r"(?im)^[ \t]*nasdaq[ :]*\r?\n[ \t]*\$?zena[ \t]*$", "", text or "")
    lines = []
    for line in text.splitlines():
        if is_branding(line):
            mark = IMAGE_MARK.search(line)
            lines.append(mark.group() if mark else "")
        else:
            lines.append(line)
    return "\n".join(lines)
