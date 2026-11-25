"""Verify only assigned claim/source pairs; use alternatives when a primary source does not settle a claim."""
import asyncio
import logging
from . import source_passages, newsroom_facts, newsroom_claim_sources, source_claim_coverage, newsroom_source_selection, source_claim_recall, source_identity


logger = logging.getLogger(__name__)


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
    for item, scores in zip(inventory, ranked):
        best = {}
        for owner, score in zip(owners, scores):
            best[owner] = max(best.get(owner, 0.), score)
        cited = [i for i in best if not newsroom_facts.is_newsroom(readings[i][0])]
        automatic = [i for i in best if newsroom_facts.is_newsroom(readings[i][0])]
        articles = [{"title": readings[i][0].title or readings[i][0].reference,
                     "as_of": readings[i][0].published, "url": readings[i][0].url} for i in automatic]
        chosen = newsroom_source_selection.candidates(item["claim"], articles, [best[i] for i in automatic],
                                                       newsroom_claim_sources.MIN_RELEVANCE)
        # Apply the same bounded product-aware selection again after reading full articles.
        out.append(sorted(cited, key=lambda i: (-best[i], i)) + [automatic[i] for i in chosen])
    return out


async def verify(inventory, readings, compare, *, discover=None, recheck=None):
    """Check cited sources and relevant newsroom evidence independently, in parallel."""
    choices = await plans(inventory, readings)
    cited = [[i for i in sources if not newsroom_facts.is_newsroom(readings[i][0])] for sources in choices]
    newsroom = [[i for i in sources if newsroom_facts.is_newsroom(readings[i][0])] for sources in choices]
    attempted = [set() for _ in inventory]
    results = await asyncio.gather(
        _verify_choices(inventory, readings, compare, cited, attempted),
        _verify_choices(inventory, readings, compare, newsroom, attempted),
    )
    errors = set().union(*(failed for failed, _ in results))
    settled = set().union(*(done for _, done in results))
    unresolved = [i for i, item in enumerate(inventory) if key(item) not in settled]
    if unresolved and recheck is None:
        pending = [inventory[i] for i in unresolved]
        seen = [attempted[i] for i in unresolved]
        try:
            recall = await source_claim_recall.choices(pending, readings, seen)
            failed, resolved = await _verify_choices(pending, readings, compare, recall, seen)
            errors |= failed
            settled |= resolved
        except Exception:
            logger.warning("Full-article recall failed for unresolved claims", exc_info=True)
            errors.update(key(item) for item in pending)
    pending = [item for item in inventory if key(item) not in settled]
    if pending and discover is not None:
        failed, resolved = await _discover(pending, readings, compare, discover)
        errors |= failed
        settled |= resolved
    pending = [item for item in inventory if key(item) not in settled]
    if pending and recheck is not None:
        try:
            failed, resolved = await recheck(pending)
            errors |= failed
            settled |= resolved
        except Exception:
            logger.warning("Combined final source check failed", exc_info=True)
            errors.update(key(item) for item in pending)
    return errors - settled


async def _verify_choices(inventory, readings, compare, choices, attempted=None):
    """Stop fallback within each source group once that group settles the claim."""
    remaining = set(range(len(inventory)))
    errors = set()
    while remaining:
        groups = {}
        for index in sorted(remaining):
            if choices[index]:
                selected = choices[index].pop(0)
                if attempted is not None:
                    attempted[index].add(selected)
                groups.setdefault(selected, []).append(index)
        if not groups:
            break
        tasks = [compare(readings[source][0], readings[source][1], [inventory[i] for i in indices])
                 if readings[source][0].status == "READ" else asyncio.sleep(0, result=None)
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


async def _discover(pending, readings, compare, discover):
    """Discover alternatives only after existing full articles fail to settle a claim."""
    try:
        extras = await discover(pending, readings)
        seen = {source_identity.url_key(c.url or c.reference) for c, _ in readings}
        start = len(readings)
        for candidate in source_identity.readings(extras):
            identity = source_identity.url_key(candidate[0].url or candidate[0].reference)
            if identity not in seen:
                readings.append(candidate)
                seen.add(identity)
        if len(readings) == start:
            return set(), set()
        plans = await source_claim_recall.choices(pending, readings, [set(range(start)) for _ in pending])
        return await _verify_choices(pending, readings, compare, plans)
    except Exception:
        logger.warning("Additional newsroom retrieval failed", exc_info=True)
        return {key(item) for item in pending}, set()
