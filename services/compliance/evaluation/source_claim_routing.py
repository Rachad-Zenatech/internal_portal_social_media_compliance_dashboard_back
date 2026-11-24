"""Verify only assigned claim/source pairs; use alternatives when a primary source does not settle a claim."""
import asyncio
from . import source_passages, newsroom_facts, newsroom_claim_sources, source_claim_coverage


def key(item):
    return source_claim_coverage.claim_key(item["claim"], item["where"])


async def plans(inventory, readings):
    passages, owners = [], []
    for index, (check, material) in enumerate(readings):
        if material is None:
            continue
        pieces = source_passages.split(material[1]) or [source_passages.Passage(
            number=1, text=material[1], section=None)]
        passages.extend(pieces)
        owners.extend([index] * len(pieces))
    ranked = await source_passages.rank([item["claim"] for item in inventory], passages)
    out = []
    for scores in ranked:
        best = {}
        for owner, score in zip(owners, scores):
            best[owner] = max(best.get(owner, 0.), score)
        cited = [i for i in best if not newsroom_facts.is_newsroom(readings[i][0])]
        automatic = [i for i in best if newsroom_facts.is_newsroom(readings[i][0])]
        peak = max((best[i] for i in automatic), default=0.)
        automatic = [i for i in automatic if newsroom_claim_sources.relevant(best[i], peak)]
        # A union of articles retrieved for different claims must not become every claim's fallback list.
        out.append(sorted(cited, key=lambda i: (-best[i], i))
                   + sorted(automatic, key=lambda i: (-best[i], i))[:2])
    return out


async def verify(inventory, readings, compare):
    """Group first-choice assignments; supported claims never get sent to unrelated additional sources."""
    choices = await plans(inventory, readings)
    remaining = set(range(len(inventory)))
    errors = set()
    while remaining:
        groups = {}
        for index in sorted(remaining):
            if choices[index]:
                groups.setdefault(choices[index].pop(0), []).append(index)
        if not groups:
            break
        tasks = [compare(readings[source][0], readings[source][1], [inventory[i] for i in indices])
                 for source, indices in groups.items()]
        answers = await asyncio.gather(*tasks, return_exceptions=True)
        for indices, answer in zip(groups.values(), answers):
            if isinstance(answer, BaseException) or answer is None:
                errors.update(indices)
                continue
            by_key = {source_claim_coverage.claim_key(c.claim, c.where): c for c in answer}
            for index in indices:
                claim = by_key.get(key(inventory[index]))
                if claim is None:
                    errors.add(index)
                elif claim.verdict in {"SUPPORTED", "CONTRADICTED"}:
                    remaining.discard(index)
                    errors.discard(index)
    return {key(inventory[i]) for i in remaining & errors}
