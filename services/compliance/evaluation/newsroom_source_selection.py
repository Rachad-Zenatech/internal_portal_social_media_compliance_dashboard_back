"""Bounded newsroom recall: exact product announcements plus general relevance."""
import re

# Product-like names, without a maintained list of brands, models or rule numbers.
_MODEL = re.compile(r"\b(?:[A-Za-z][A-Za-z-]{2,}[ -]+\d{2,}[A-Za-z0-9-]*|[A-Z]{2,5} +[A-Z][a-z]+)\b")
PRODUCT_LIMIT = 3
GENERAL_LIMIT = 2


def product_match(query, title):
    for model in _MODEL.findall(title or ""):
        pattern = r"\b" + re.escape(model).replace(r"\ ", r"[ -]*") + r"\b"
        if re.search(pattern, query, re.IGNORECASE):
            return True
    return False


def _date(row):
    return str(row.get("as_of") or "")[:10]


def candidates(query, rows, scores, minimum):
    """Protect recent exact-product titles from summary keyword competition; at most five articles per claim."""
    focused = [i for i, row in enumerate(rows) if product_match(query, row.get("title"))]
    focused.sort(key=lambda i: (_date(rows[i]), scores[i], rows[i].get("url") or rows[i]["title"]), reverse=True)
    peak = max(scores, default=0.)
    general = [i for i, score in enumerate(scores) if score >= minimum and score >= peak * .6]
    general.sort(key=lambda i: (scores[i], _date(rows[i]), rows[i].get("url") or rows[i]["title"]), reverse=True)
    return list(dict.fromkeys([*focused[:PRODUCT_LIMIT], *general[:GENERAL_LIMIT]]))
