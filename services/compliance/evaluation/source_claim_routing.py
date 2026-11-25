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
    """Check cited sources and relevant newsroom evidence independently, in parallel."""
    choices = await plans(inventory, readings)
    cited = [[i for i in sources if not newsroom_facts.is_newsroom(readings[i][0])] for sources in choices]
    newsroom = [[i for i in sources if newsroom_facts.is_newsroom(readings[i][0])] for sources in choices]
    results = await asyncio.gather(
        _verify_choices(inventory, readings, compare, cited),
        _verify_choices(inventory, readings, compare, newsroom),
    )
    errors = set().union(*(failed for failed, _ in results))
    settled = set().union(*(done for _, done in results))
    return errors - settled


async def _verify_choices(inventory, readings, compare, choices):
    """Stop fallback within each source group once that group settles the claim."""
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
    failed = {key(inventory[i]) for i in remaining & errors}
    settled = {key(inventory[i]) for i in set(range(len(inventory))) - remaining}
    return failed, settled
