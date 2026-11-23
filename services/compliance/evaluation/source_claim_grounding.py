"""Recover verbatim post excerpts when a model changes only spacing or list separators."""
import re

from services.compliance.evaluation.image_marks import IMAGE_MARK

_TOKEN = re.compile(r"\d+(?:[.,]\d+)*|[^\W\d_]+(?:['’][^\W\d_]+)*|[^\s]", re.UNICODE)
_LIST_SEPARATORS = {",", ";", ":", "•", "·"}


def _tokens(text):
    return [(m.group().casefold().replace("’", "'"), m.start(), m.end())
            for m in _TOKEN.finditer(text) if m.group() not in _LIST_SEPARATORS]


def same_assertion(original, returned):
    """Verification must cover the entire saved assertion, allowing only formatting differences."""
    original_tokens = [token for token, _, _ in _tokens(original)]
    return bool(original_tokens) and original_tokens == [token for token, _, _ in _tokens(returned)]


def excerpts(post, claim, max_chars=400):
    """Require the same contiguous tokens in one location; return the original spelling and punctuation.

    Numbers, decimal points, signs, words, qualifiers and order must match. Matching never spans a
    caption/image boundary. Inserted list separators and whitespace cannot change the saved assertion.
    """
    wanted = [token for token, _, _ in _tokens(claim)]
    if not wanted or len(claim) > max_chars:
        return []
    matches = []
    for number, body in enumerate(IMAGE_MARK.split(post)):
        tokens = _tokens(body)
        values = [token for token, _, _ in tokens]
        for start in range(len(tokens) - len(wanted) + 1):
            if values[start:start + len(wanted)] != wanted:
                continue
            original = body[tokens[start][1]:tokens[start + len(wanted) - 1][2]]
            if len(original) <= max_chars:
                matches.append((original, "caption" if number == 0 else "image"))
            break
    return matches
