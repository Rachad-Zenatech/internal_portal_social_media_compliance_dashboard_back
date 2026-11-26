"""Keep grounded, atomic claim excerpts and stable identities without dropping qualifications."""
import hashlib
import re

from .image_marks import IMAGE_MARK
from .source_claim_grounding import _tokens

_BREAK = re.compile(r"(?<=[.!?])\s+(?=[A-Z])")
_VERB = re.compile(r"\b(?:under (?:development|testing|evaluation)|is|are|was|were|has|have|can|may|will|does|do|did|must|should|establish(?:es)?|needs?|requires?|supports?|provides?|delivers?|transports?|reduces?|increases?|offers?|meets?|contains?)\b", re.I)
_QUALIFIER = re.compile(r"^(?:it|its|this|these|those|that|they|however|but|only|not|unless|except|provided|subject to|for example|in particular)\b", re.I)
_ABBREVIATION = re.compile(r"(?:\b(?:[A-Za-z]\.){2,}|\b[A-Z]\.|\b(?:Inc|Corp|Ltd|Dr|Mr|Ms|Mrs|No|Fig|e\.g|i\.e)\.)$", re.I)


def atomic_spans(text):
    starts = [0]
    for boundary in _BREAK.finditer(text):
        if not _ABBREVIATION.search(text[:boundary.start()]):
            starts.append(boundary.end())
    pieces = [(start, starts[i + 1] if i + 1 < len(starts) else len(text)) for i, start in enumerate(starts)]
    kept = []
    for start, end in pieces:
        while end > start and text[end - 1].isspace():
            end -= 1
        current = text[start:end]
        if kept and _VERB.search(text[kept[-1][0]:kept[-1][1]]) and _VERB.search(current) and not _QUALIFIER.match(current):
            kept.append((start, end))
        elif kept:
            kept[-1] = (kept[-1][0], end)
        else:
            kept.append((start, end))
    return kept


def identity(claim, where):
    words = [word for word, _, _ in _tokens(claim)]
    return "claim-" + hashlib.sha256((where + "|" + repr(words)).encode()).hexdigest()[:24]


def grounded(post, excerpt, where):
    markers = list(IMAGE_MARK.finditer(post))
    boundaries = [(0, markers[0].start() if markers else len(post), "caption", None)]
    boundaries += [(m.end(), markers[i + 1].start() if i + 1 < len(markers) else len(post), "image", int(re.search(r"\d+", m.group()).group()))
                   for i, m in enumerate(markers)]
    for start, end, location, image in boundaries:
        if where != location:
            continue
        offset = post.find(excerpt, start, end)
        if offset < 0:
            continue
        return [{"claim": excerpt[a:z], "where": where, "claim_id": identity(excerpt[a:z], where),
                 "post_start": offset + a, "post_end": offset + z, "image_index": image,
                 "context": excerpt} for a, z in atomic_spans(excerpt)]
    return []
