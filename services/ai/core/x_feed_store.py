"""x_feed_data: where the scraped X / news feed is kept, in the vector database (MongoDB Atlas, the same database and
connection as the vector store) instead of a JSON file on disk. One scrape is one "run" of documents:

    feed:current           kind FEED        the pointer: which run is current, when it was written and collected,
                                            how the collection went (status per source), and where it came from
    <run>:topic:<topic>    kind FEED_TOPIC  one per topic: name, description, keywords (and the scraper's own summary)
    <run>:item:<topic>:<n> kind FEED_ITEM   one per collected headline or post, with its sentiment

A scrape writes its documents first, then moves the pointer to them, then removes the previous run, so a reader
always sees one complete feed (never half of a new one). Only the latest run is kept: the history of what was
collected lives in Postgres (listening_items) and the Atlas evidence index.

Off until MONGODB_URI is set (is_enabled() is False and nothing here does anything), like the rest of the vector store.
"""

import logging
import os
import uuid
from collections import defaultdict
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from services.ai.core import vector_store_service as store

logger = logging.getLogger(__name__)

POINTER_ID = "feed:current"
TOPIC_KIND, ITEM_KIND, POINTER_KIND = "FEED_TOPIC", "FEED_ITEM", "FEED"
_BOOKKEEPING = {"_id", "kind", "run_id", "order", "seq", "topic_external_id"}

# The current run's feed as read, so the several reads one refresh makes (a snapshot per topic) cost one query each
# for the pointer rather than a full read.
_cache: Dict[str, Any] = {"run_id": None, "payload": None}


def collection_name() -> str:
    return os.getenv("X_FEED_COLLECTION", "x_feed_data").strip() or "x_feed_data"


def is_enabled() -> bool:
    return store.is_enabled()


def _collection():
    return store.named_collection(collection_name())


def _aware(value: Optional[datetime]) -> Optional[datetime]:
    """Mongo hands datetimes back without a zone; they were stored as UTC."""
    if value is not None and value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value


def _clean(doc: Dict[str, Any]) -> Dict[str, Any]:
    return {k: v for k, v in doc.items() if k not in _BOOKKEEPING}


async def write_feed(payload: Dict[str, Any]) -> Dict[str, Any]:
    """Store a collected feed ({"source", "collected_at", "collection", "topics": [{..., "comments": [...]}]}) as the
    current one. Raises if the vector database isn't configured or can't be written."""
    col = _collection()
    if col is None:
        raise RuntimeError("The vector database isn't configured (MONGODB_URI), so the feed can't be stored there.")
    run_id = uuid.uuid4().hex
    now = datetime.now(timezone.utc)
    docs: List[Dict[str, Any]] = []
    items = 0
    for order, topic in enumerate(payload.get("topics") or []):
        ext = str(topic["external_id"])
        docs.append({**{k: v for k, v in topic.items() if k != "comments"},
                     "_id": f"{run_id}:topic:{ext}", "kind": TOPIC_KIND, "run_id": run_id, "order": order,
                     "external_id": ext})
        for seq, item in enumerate(topic.get("comments") or []):
            docs.append({**item, "_id": f"{run_id}:item:{ext}:{seq}", "kind": ITEM_KIND, "run_id": run_id,
                         "topic_external_id": ext, "seq": seq})
            items += 1
    pointer = {
        "_id": POINTER_ID, "kind": POINTER_KIND, "run_id": run_id, "written_at": now,
        "collected_at": payload.get("collected_at"), "source": payload.get("source"),
        "is_live_scraped": bool(payload.get("is_live_scraped")), "collection": payload.get("collection"),
        "topic_count": len(payload.get("topics") or []), "item_count": items,
    }
    await col.create_index([("run_id", 1), ("kind", 1)])  # a no-op once it exists
    try:
        if docs:
            await col.insert_many(docs, ordered=False)
    except Exception:
        await col.delete_many({"run_id": run_id})  # don't leave half a run behind
        raise
    await col.replace_one({"_id": POINTER_ID}, pointer, upsert=True)  # readers switch to the new run here
    removed = await col.delete_many({"kind": {"$in": [TOPIC_KIND, ITEM_KIND]}, "run_id": {"$ne": run_id}})
    _cache.update(run_id=None, payload=None)
    return {"run_id": run_id, "topics": pointer["topic_count"], "items": items, "removed_previous": removed.deleted_count}


async def read_feed() -> Optional[Dict[str, Any]]:
    """The current feed in the shape the scraper produced it, or None if none has been stored (or the database isn't
    configured). Only the pointer is read when the run hasn't changed since the last read."""
    col = _collection()
    if col is None:
        return None
    pointer = await col.find_one({"_id": POINTER_ID})
    if not pointer:
        return None
    if _cache["run_id"] == pointer["run_id"]:
        return _cache["payload"]
    run_id = pointer["run_id"]
    topics: List[Dict[str, Any]] = []
    async for d in col.find({"kind": TOPIC_KIND, "run_id": run_id}).sort("order", 1):
        topics.append(_clean(d))
    by_topic: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    async for d in col.find({"kind": ITEM_KIND, "run_id": run_id}).sort([("topic_external_id", 1), ("seq", 1)]):
        by_topic[d["topic_external_id"]].append(_clean(d))
    for t in topics:
        t["comments"] = by_topic.get(t["external_id"], [])
    payload = {
        "source": pointer.get("source"), "is_live_scraped": pointer.get("is_live_scraped", False),
        "collected_at": pointer.get("collected_at"), "collection": pointer.get("collection"),
        "written_at": _aware(pointer.get("written_at")), "run_id": run_id, "topics": topics,
    }
    _cache.update(run_id=run_id, payload=payload)
    return payload


async def written_at() -> Optional[datetime]:
    """When the current feed was stored (the pointer only), for deciding whether topics need syncing again."""
    col = _collection()
    if col is None:
        return None
    pointer = await col.find_one({"_id": POINTER_ID}, {"written_at": 1})
    return _aware(pointer.get("written_at")) if pointer else None


async def status() -> Dict[str, Any]:
    """What is stored, for diagnostics (no document contents)."""
    col = _collection()
    if col is None:
        return {"enabled": False, "collection": collection_name()}
    pointer = await col.find_one({"_id": POINTER_ID}, {"_id": 0, "run_id": 1, "written_at": 1, "collected_at": 1,
                                                       "topic_count": 1, "item_count": 1, "collection.status": 1})
    return {"enabled": True, "collection": collection_name(), "current": pointer or None,
            "documents": await col.count_documents({})}
