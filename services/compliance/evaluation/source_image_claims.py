"""Keep short capability lists visible, and require evidence about the specific capability."""
import re

from .image_marks import IMAGE_MARK
from services.compliance.analytics.post_signals import URL_PATTERN

# Connectors separate fragments of body copy from the short nominal labels used in feature lists.
FRAGMENT_WORDS = frozenset("a an the can could may might must will would should is are was were be been being it its this these those how why who which that while when where in into on at of to for from with without by or".split())
COUNT_WORDS = frozenset("zero one two three four five six seven eight nine ten eleven twelve thirteen fourteen fifteen sixteen seventeen eighteen nineteen twenty thirty forty fifty sixty seventy eighty ninety hundred thousand million billion".split())
GENERIC_WORDS = frozenset("and support supports capability capabilities feature features".split())


def words(text):
    return re.findall(r"[a-z]+", text.casefold())


def is_identifier(text):
    """A standalone website, handle, or brand mark makes no factual assertion."""
    from .branding import is_branding

    text = (text or "").strip().strip(".,!?;:\u0022\u0027\u201c\u201d\u2018\u2019")
    return (
        is_branding(text)
        or URL_PATTERN.fullmatch(text) is not None
        or re.fullmatch(r"@[\w.]+", text) is not None
    )


def label(text):
    text = re.sub(r"^\s*(?:[-*?]+|\d+[.)])\s*", "", text).strip()
    tokens = words(text)
    if (is_identifier(text) or not 2 <= len(tokens) <= 6 or len(text) > 70 or re.search(r"[.,!?;:=/\d]", text)
            or set(tokens) & (FRAGMENT_WORDS | COUNT_WORDS)):
        return None
    return text


def labels(text):
    """Adjacent short labels form a list; wrapped sentences, logos and source metadata do not."""
    found, group, previous = [], [], ""
    for line in [*IMAGE_MARK.sub("", text).splitlines(), ""]:
        candidate = label(line)
        preceding = words(previous)
        if candidate and line[:1].islower() and preceding and preceding[-1] in FRAGMENT_WORDS:
            candidate = None  # a wrapped sentence ending "to", "in", etc. continues on this line
        previous = line
        if candidate:
            group.append(candidate)
        else:
            if len(group) >= 2:
                found.extend(group)
            group = []
    return list(dict.fromkeys(found))


def specific_words(text):
    return {word[:5] for word in words(text) if word not in GENERIC_WORDS}


def covers(statement, passage):
    wanted = specific_words(statement)
    return bool(wanted) and wanted <= specific_words(passage)


def evidence_covers(claim, evidence, source_text):
    """Conservatively leave a short image capability unresolved if its evidence addresses something else."""
    return label(claim) is None or (covers(claim, evidence) and covers(claim, source_text))



def guard_claims(body, claims, source_text):
    required = labels(body)
    out = []
    for claim in claims:
        unsupported = [item for item in required if covers(item, claim.claim)
                       and not (covers(item, claim.evidence or "") and covers(item, source_text))]
        if claim.verdict == "SUPPORTED" and unsupported:
            claim = claim.model_copy(update={"verdict": "UNCLEAR", "note":
                "The evidence does not establish these specific capabilities: " + ", ".join(unsupported) + ". Confirm them against a supporting passage."})
        out.append(claim)
    return out
