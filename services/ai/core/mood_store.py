"""The collections public mood learns from, in the same MongoDB database as the vector store. They are ordinary
collections (no vector index):

    mood_assessments           one record per assessment of a draft, with the evidence it used and the measurements
                               (features) it was described by at that moment. Insert-only: reassessing a draft adds a
                               record; nothing is overwritten.
    mood_reviews               a reviewer's decision on an assessment (POST / WAIT / HOLD) and why
    mood_match_corrections     a reviewer's corrections to the matching ("unrelated") and to the sentiment, kept apart
                               from the decision because they improve different things
    training_datasets          one manifest per fixed training dataset
    training_dataset_examples  the examples of a dataset, one record each (a dataset can be large)
    model_runs                 every training run: dataset, model and feature versions, metrics, model file, status

Off until MONGODB_URI is set (is_enabled() is False and the helpers do nothing), like the rest of the vector store.
"""

import logging
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Sequence, Tuple

from services.ai.core import vector_store_service as store

logger = logging.getLogger(__name__)

ASSESSMENTS = "mood_assessments"
REVIEWS = "mood_reviews"
MATCH_CORRECTIONS = "mood_match_corrections"
DATASETS = "training_datasets"
EXAMPLES = "training_dataset_examples"
MODEL_RUNS = "model_runs"
AUTO_TRAINING = "mood_auto_training"  # one record: what the last automatic training attempt did, and on what data

_INDEXES: Dict[str, List[Sequence[Tuple[str, int]]]] = {
    ASSESSMENTS: [[("submission_id", 1), ("assessed_at", -1)], [("assessed_at", 1)], [("feature_schema_version", 1)]],
    REVIEWS: [[("assessment_id", 1), ("reviewer_id", 1)]],
    MATCH_CORRECTIONS: [[("assessment_id", 1)], [("item_id", 1)]],
    EXAMPLES: [[("dataset_version", 1), ("split", 1)]],
    MODEL_RUNS: [[("dataset_version", 1)], [("status", 1), ("created_at", -1)]],
}
_indexed: set = set()


def is_enabled() -> bool:
    return store.is_enabled()


def collection(name: str):
    return store.named_collection(name)


def now() -> datetime:
    return datetime.now(timezone.utc)


def aware(value: Optional[datetime]) -> Optional[datetime]:
    """Mongo hands datetimes back without a zone; they were stored as UTC."""
    return value.replace(tzinfo=timezone.utc) if isinstance(value, datetime) and value.tzinfo is None else value


async def ensure_indexes(name: str) -> None:
    """The indexes a collection needs, created once per process (creating an existing one is a no-op)."""
    col = collection(name)
    if col is None or name in _indexed:
        return
    for keys in _INDEXES.get(name, []):
        await col.create_index(list(keys))
    _indexed.add(name)


async def insert_once(name: str, doc: Dict[str, Any]) -> bool:
    """Add a record that must never be replaced. False if one with this _id already exists."""
    from pymongo.errors import DuplicateKeyError

    col = collection(name)
    if col is None:
        raise RuntimeError("The vector database isn't configured (MONGODB_URI).")
    await ensure_indexes(name)
    try:
        await col.insert_one(doc)
        return True
    except DuplicateKeyError:
        return False


async def upsert(name: str, doc: Dict[str, Any]) -> None:
    col = collection(name)
    if col is None:
        raise RuntimeError("The vector database isn't configured (MONGODB_URI).")
    await ensure_indexes(name)
    await col.replace_one({"_id": doc["_id"]}, doc, upsert=True)


async def get(name: str, doc_id: str) -> Optional[Dict[str, Any]]:
    col = collection(name)
    return await col.find_one({"_id": doc_id}) if col is not None else None


async def update(name: str, doc_id: str, fields: Dict[str, Any]) -> bool:
    col = collection(name)
    if col is None:
        return False
    return (await col.update_one({"_id": doc_id}, {"$set": fields})).matched_count > 0


async def find(name: str, flt: Optional[Dict[str, Any]] = None, sort: Optional[Sequence[Tuple[str, int]]] = None,
               limit: int = 0) -> List[Dict[str, Any]]:
    col = collection(name)
    if col is None:
        return []
    cursor = col.find(flt or {})
    if sort:
        cursor = cursor.sort(list(sort))
    if limit:
        cursor = cursor.limit(limit)
    return [d async for d in cursor]


async def count(name: str, flt: Optional[Dict[str, Any]] = None) -> int:
    col = collection(name)
    return await col.count_documents(flt or {}) if col is not None else 0
