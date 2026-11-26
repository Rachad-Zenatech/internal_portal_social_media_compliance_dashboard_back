"""Bounded second-pass retrieval over full articles already read, only for unresolved claims."""
from . import source_passages


async def choices(inventory, readings, attempted):
    passages, owners = [], []
    for index, (check, material) in enumerate(readings):
        if check.status != "READ" or material is None:
            continue
        chunks = source_passages.split(material[1]) or [source_passages.Passage(number=1, text=material[1], section=None)]
        passages.extend(chunks)
        owners.extend([index] * len(chunks))
    if not passages:
        return [[] for _ in inventory]
    scores = await source_passages.rank([item["claim"] for item in inventory], passages)
    plans = []
    for seen, row in zip(attempted, scores):
        best = {}
        for index, score in zip(owners, row):
            if index not in seen and score > 0:
                best[index] = max(best.get(index, 0.), score)
        # Unlike initial summary selection, the best positive full-text matches get a
        # bounded opportunity. Relevance selects evidence; it never establishes support.
        plans.append(sorted(best, key=lambda index: (-best[index], index))[:2])
    return plans
