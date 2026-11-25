"""Ground source quotations separately from a model's semantic explanation."""
import re
import unicodedata


def normalized(text):
    return " ".join(unicodedata.normalize("NFKC", text).casefold().split())


def quotes(result, source):
    """Keep only real source excerpts; do not turn a located passage into a new verdict."""
    body = normalized(source)
    requested = getattr(result, "evidence_quotes", [])
    if any(len(re.findall(r"\w+", q)) < 4 or normalized(q) not in body for q in requested):
        return []
    return list(dict.fromkeys(q.strip() for q in requested))[:3]
