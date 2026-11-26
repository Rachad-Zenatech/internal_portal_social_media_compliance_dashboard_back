"""Collect bounded full-text evidence; accept only exact, source-attributed quotations."""
import re
from collections import Counter
from . import source_passages, source_claim_checks, source_claim_coverage

MAX_EVIDENCE_CHARS = 30000
MAX_EXCERPT_CHARS = 2000
_AUXILIARY = re.compile(r"\b(?:do|does|did|is|are|was|were|has|have|had|be|been|being|the|a|an|this|that|it|its|of|for|and|to|in|on|with)\b", re.IGNORECASE)


def context(post, item):
    if item.get("context"):
        return item["context"][:800]
    start = item.get("post_start")
    if not isinstance(start, int) or not 0 <= start < len(post):
        start = post.find(item["claim"])
    if start < 0:
        return item["claim"]
    boundaries = [0, *[match.start() for match in re.finditer(r"(?m)^Text in image \d+:", post)], len(post)]
    low = max(offset for offset in boundaries if offset <= start)
    high = min((offset for offset in boundaries if offset > start), default=len(post))
    return post[max(low, start - 250):min(high, start + len(item["claim"]) + 250)]


def _windows(chunks):
    windows, group, length = [], [], 0
    for passage in chunks:
        shown = ((passage.section + "\n") if passage.section else "") + passage.text
        if group and length + len(shown) > MAX_EXCERPT_CHARS:
            windows.append(group)
            group, length = [], 0
        group.append(passage)
        length += len(shown)
        if length >= 900:
            windows.append(group)
            group, length = [], 0
    if group:
        windows.append(group)
    return windows


async def prepare(post, inventory, readings):
    passages, owners, by_source = [], [], {}
    for index, (check, material) in enumerate(readings):
        if check.status != "READ" or material is None:
            continue
        chunks = source_passages.split(material[1]) or [source_passages.Passage(number=1, text=material[1], section=None)]
        windows = _windows(chunks)
        by_source[index] = windows
        for offset, window in enumerate(windows):
            text = "\n".join(((p.section + "\n") if p.section else "") + p.text for p in window)
            passages.append(source_passages.Passage(number=len(passages) + 1, text=text, section=None))
            owners.append((index, offset))
    if not passages:
        return []
    # Rank the assertion independently; broad image context must not displace its direct evidence.
    queries = [item["claim"] for item in inventory] + [context(post, item) for item in inventory]
    df = Counter(term for passage in passages for term in set(source_passages._terms(passage.text)))
    ranked = [source_passages._keyword_scores(_AUXILIARY.sub(" ", query), passages, df) for query in queries]
    offers = {}
    budget = MAX_EVIDENCE_CHARS // max(1, len(inventory))
    for claim_index in range(len(inventory)):
        plain, contextual = ranked[claim_index], ranked[claim_index + len(inventory)]
        # Cover two direct passages plus a context passage per source, within the same total prompt budget.
        choices = []
        for source in by_source:
            indices = [i for i, owner in enumerate(owners) if owner[0] == source]
            direct = sorted(indices, key=lambda i: (-plain[i], i))[:2]
            contextual_best = max(indices, key=lambda i: contextual[i])
            chosen = list(dict.fromkeys([*direct, contextual_best]))
            relevance = max(plain[i] for i in indices) * .8 + max(contextual[i] for i in indices) * .2
            choices.extend((-relevance, tier, i) for tier, i in enumerate(chosen) if plain[i] > 0 or contextual[i] > 0)
        used = 0
        for _, _, index in sorted(choices):
            source, offset = owners[index]
            window = by_source[source][offset]
            excerpt_id = f"paragraphs-{window[0].number}-{window[-1].number}"
            key = source, excerpt_id
            if key in offers:
                continue
            remaining = min(MAX_EXCERPT_CHARS, budget - used)
            if remaining < 100:
                break
            text = passages[index].text
            if len(text) > remaining:
                continue
            check = readings[source][0]
            offers[key] = {"source_id": f"S{source + 1}", "source_index": source, "excerpt_id": excerpt_id,
                           "title": check.title or check.reference, "url": check.url, "published": check.published,
                           "location": f"{window[0].label} through {window[-1].label}", "text": text}
            used += len(text)
    return list(offers.values())


def _normal(text):
    return " ".join(text.split())


def apply(answers, offers, readings, model, *, commit=True):
    allowed = {(offer["source_id"], offer["excerpt_id"]): offer for offer in offers}
    resolved, valid = set(), True
    for answer in answers:
        if answer.verdict != "SUPPORTED":
            continue
        offer = allowed.get((answer.source_id, answer.excerpt_id))
        quote = (answer.source_quote or "").strip()
        if offer is None or len(quote) < 15 or len(quote) > 1200:
            valid = False
            continue
        index = offer["source_index"]
        check, material = readings[index]
        if check.status != "READ" or material is None or _normal(quote) not in _normal(offer["text"]) or _normal(quote) not in _normal(material[1]):
            valid = False
            continue
        # Existing figure, confidence and image-capability safeguards still apply, against this excerpt only.
        grounded = answer.model_copy(update={"evidence": answer.evidence or quote, "evidence_quotes": [quote],
                                             "source_location": offer["location"]})
        claims = source_claim_checks._to_claims([grounded], offer["text"])
        if not claims or claims[0].verdict != "SUPPORTED":
            valid = False
            continue
        claim = claims[0].model_copy(update={"source_quote": quote, "source_location": offer["location"],
                                            "source_url": check.url})
        key = source_claim_coverage.claim_key(claim.claim, claim.where)
        if commit:
            check.claims = [previous for previous in check.claims
                            if source_claim_coverage.claim_key(previous.claim, previous.where) != key]
            check.claims.append(claim)
            check.model = model
            check.message = None
        resolved.add(key)
    return resolved, valid
