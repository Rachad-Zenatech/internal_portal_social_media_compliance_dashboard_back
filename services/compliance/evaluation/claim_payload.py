"""Bound each AI request while covering all passages/claims in successive batches."""
import json
import os

MAX_CHARS = int(os.getenv("SOURCE_CHECK_MAX_POST_CHARS", "12000"))
MAX_ITEMS = max(1, min(6, int(os.getenv("SOURCE_CHECK_MAX_CLAIMS", "12"))))


def batches(items, max_items=MAX_ITEMS):
    batch, size = [], 0
    for item in items:
        cost = len(json.dumps(item, ensure_ascii=False))
        if batch and (len(batch) >= max_items or size + cost > MAX_CHARS // 2):
            yield batch
            batch, size = [], 0
        batch.append(item)
        size += cost
    if batch:
        yield batch


def payload(context, name, items, instruction=""):
    """Keep structured candidates intact. Omitted context stays in the saved local inventory."""
    context_original = context
    data = {"post_context": "", "context_truncated": True, name: items}
    empty = instruction + json.dumps(data, ensure_ascii=False)
    room = MAX_CHARS - len(empty)
    if room < 0:
        return None  # an oversized individual passage must stay incomplete, never be silently cut into a new claim
    if len(context) > room:
        # Both opening attribution and closing qualifications matter; every passage is also read in its own batch.
        left = room // 2
        context = context[:left] + (context[-(room-left):] if room-left else "")
    data["context_truncated"] = len(context) < len(context_original)
    data["post_context"] = context
    shown = instruction + json.dumps(data, ensure_ascii=False)
    # JSON escaping can increase length beyond the character count of the context.
    while len(shown) > MAX_CHARS and data["post_context"]:
        data["context_truncated"] = True
        data["post_context"] = data["post_context"][:-(len(shown)-MAX_CHARS)]
        shown = instruction + json.dumps(data, ensure_ascii=False)
    return shown if len(shown) <= MAX_CHARS else None
