"""Vector store: MongoDB Atlas vector search for reference material the content analysis can draw on
(brand voice, company facts, platform norms, audience profiles and, later, past review decisions).

Same setup as the Finance portal: MONGODB_URI, the shared `zenatech_ai` database, Gemini embeddings
(`gemini-embedding-2`) and an Atlas `vector_index`. This project keeps its documents in its own collection, so it
never reads or changes other portals' data.

Off until MONGODB_URI is set: is_enabled() is False, search() returns nothing and writes are skipped, so nothing
that uses it can fail because the vector store isn't configured.

Document shape (one per piece of reference material):
    _id         "<kind>:<source id>", e.g. "BRAND_VOICE:3f2c..."
    kind        BRAND_VOICE | FACT | PLATFORM_NORM | AUDIENCE_PROFILE | REVIEW_DECISION ...
    title, text the passage itself (text is what gets embedded)
    brands, audiences, platforms   scope; ["*"] means every value, so filters can match "this brand or all brands"
    meta        anything else the caller needs back with a match (e.g. a review's decision and notes); kept up to
                date on every upsert, even when the text (and so the embedding) is unchanged
    content_hash  skips re-embedding text that hasn't changed
    embedding   the Gemini embedding vector
"""

import asyncio
import hashlib
import logging
import os
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, Iterable, List, Optional

from google import genai
from google.genai import types

from services.ai_observability import span

logger = logging.getLogger(__name__)

ALL = "*"
EMBED_BATCH = 100  # texts per embedding request
FILTER_PATHS = ["kind", "brands", "audiences", "platforms"]


def _setting(name: str, default: str) -> str:
    return os.getenv(name, default).strip() or default


def database_name() -> str:
    return _setting("VECTOR_MONGO_DB", "zenatech_ai")


def collection_name() -> str:
    return _setting("VECTOR_MONGO_COLLECTION", "social_media_compliance_context")


def index_name() -> str:
    return _setting("VECTOR_INDEX_NAME", "vector_index")


def embedding_model() -> str:
    return _setting("VECTOR_EMBEDDING_MODEL", "gemini-embedding-2")


def is_enabled() -> bool:
    return bool(os.getenv("MONGODB_URI", "").strip())


_client = None
_gemini: Optional[genai.Client] = None


def _collection():
    """The project's collection, or None when MONGODB_URI isn't set."""
    global _client
    if not is_enabled():
        return None
    if _client is None:
        import motor.motor_asyncio  # imported lazily: only needed once the vector store is configured

        _client = motor.motor_asyncio.AsyncIOMotorClient(os.environ["MONGODB_URI"], serverSelectionTimeoutMS=5000)
    return _client.get_database(database_name()).get_collection(collection_name())


def _gemini_client() -> genai.Client:
    global _gemini
    if _gemini is None:
        api_key = os.getenv("GEMINI_API_KEY")
        if not api_key:
            raise RuntimeError("GEMINI_API_KEY is not configured, so text can't be embedded.")
        _gemini = genai.Client(api_key=api_key)
    return _gemini


async def embed(texts: List[str]) -> List[List[float]]:
    """Gemini embeddings for the texts, in order, batched to keep requests few."""
    vectors: List[List[float]] = []
    client = _gemini_client()
    for start in range(0, len(texts), EMBED_BATCH):
        batch = texts[start:start + EMBED_BATCH]
        # One Content per text: gemini-embedding-2 is multimodal and would merge a plain list of strings into a
        # single input with one embedding.
        contents = [types.Content(parts=[types.Part(text=t)]) for t in batch]
        async with span("embedding", "gemini.embed_content", model=embedding_model(), texts=len(batch),
                        chars=sum(len(t) for t in batch)):
            res = await client.aio.models.embed_content(model=embedding_model(), contents=contents)
        vectors += [list(e.values or []) for e in res.embeddings or []]
    if len(vectors) != len(texts):
        raise RuntimeError(f"Expected {len(texts)} embeddings, got {len(vectors)}")
    return vectors


def _scope(values: Optional[Iterable[str]]) -> List[str]:
    """An empty scope means every value."""
    cleaned = sorted({v for v in (values or []) if v})
    return cleaned or [ALL]


def _hash(text: str, title: str) -> str:
    return hashlib.sha256(f"{embedding_model()}\n{title}\n{text}".encode("utf-8")).hexdigest()


@dataclass
class VectorDoc:
    kind: str
    source_id: str
    text: str
    title: str = ""
    brands: List[str] = field(default_factory=list)
    audiences: List[str] = field(default_factory=list)
    platforms: List[str] = field(default_factory=list)
    meta: Dict[str, Any] = field(default_factory=dict)

    @property
    def doc_id(self) -> str:
        return f"{self.kind}:{self.source_id}"


async def upsert(docs: List[VectorDoc]) -> Dict[str, int]:
    """Add or update documents. Only new or changed text is embedded; scope and meta are refreshed on every
    document either way; everything is written in one bulk call."""
    collection = _collection()
    if collection is None or not docs:
        return {"embedded": 0, "unchanged": 0}
    from pymongo import UpdateOne

    hashes = {d.doc_id: _hash(d.text, d.title) for d in docs}
    existing = {
        row["_id"]: row.get("content_hash")
        async for row in collection.find({"_id": {"$in": list(hashes)}}, {"content_hash": 1})
    }
    changed = [d for d in docs if existing.get(d.doc_id) != hashes[d.doc_id]]
    vectors = dict(zip(
        (d.doc_id for d in changed),
        await embed([f"{d.title}\n{d.text}".strip() for d in changed]) if changed else [],
    ))
    now = datetime.now(timezone.utc)
    ops = []
    for d in docs:
        fields: Dict[str, Any] = {
            "kind": d.kind, "source_id": d.source_id, "title": d.title, "text": d.text,
            "brands": _scope(d.brands), "audiences": _scope(d.audiences), "platforms": _scope(d.platforms),
            "meta": d.meta, "updated_at": now,
        }
        if d.doc_id in vectors:
            fields.update(content_hash=hashes[d.doc_id], embedding=vectors[d.doc_id], embedding_model=embedding_model())
        ops.append(UpdateOne({"_id": d.doc_id}, {"$set": fields}, upsert=True))
    await collection.bulk_write(ops, ordered=False)
    if any(d.kind != "REVIEW_DECISION" for d in changed):
        await _reference_changed()
    return {"embedded": len(changed), "unchanged": len(docs) - len(changed)}


async def delete(doc_ids: List[str]) -> int:
    collection = _collection()
    if collection is None or not doc_ids:
        return 0
    res = await collection.delete_many({"_id": {"$in": doc_ids}})
    if res.deleted_count and any(not i.startswith("REVIEW_DECISION:") for i in doc_ids):
        await _reference_changed()
    return res.deleted_count


async def _reference_changed() -> None:
    """Assistant answers may quote reference material, so cached ones are dropped when it changes (past review
    decisions aren't shown to the assistant, so those don't count)."""
    from services import semantic_cache
    try:
        await semantic_cache.clear("CHAT")
    except Exception as e:
        logger.warning("Couldn't clear cached assistant answers after a reference material change: %s", e)


async def search(
    query: str,
    *,
    kinds: Optional[List[str]] = None,
    brand: Optional[str] = None,
    audience: Optional[str] = None,
    platforms: Optional[List[str]] = None,
    limit: int = 5,
    min_score: float = 0.0,
) -> List[Dict[str, Any]]:
    """The passages closest in meaning to the query, limited to those scoped to this brand / audience / platforms
    (or to all of them). Returns [] when the store is off or anything goes wrong, so callers can always go ahead."""
    collection = _collection()
    if collection is None or not query.strip():
        return []
    filters: List[Dict[str, Any]] = []
    if kinds:
        filters.append({"kind": {"$in": kinds}})
    if brand:
        filters.append({"brands": {"$in": [brand, ALL]}})
    if audience:
        filters.append({"audiences": {"$in": [audience, ALL]}})
    if platforms:
        filters.append({"platforms": {"$in": [*platforms, ALL]}})
    try:
        async with span("retrieval", "vector_store.search", kinds=kinds, limit=limit, min_score=min_score) as s:
            query_vector = (await embed([query]))[0]
            stage: Dict[str, Any] = {
                "index": index_name(), "path": "embedding", "queryVector": query_vector,
                "numCandidates": max(50, limit * 10), "limit": limit,
            }
            if filters:
                stage["filter"] = {"$and": filters} if len(filters) > 1 else filters[0]
            pipeline = [
                {"$vectorSearch": stage},
                {"$project": {"kind": 1, "title": 1, "text": 1, "source_id": 1, "meta": 1, "brands": 1,
                              "score": {"$meta": "vectorSearchScore"}}},
            ]
            rows = await collection.aggregate(pipeline).to_list(length=limit)
            found = [r for r in rows if r.get("score", 0) >= min_score]
            s.attributes.update(results=len(found), top_score=round(found[0]["score"], 4) if found else None)
            return found
    except Exception as e:
        logger.warning("Vector search failed (continuing without it): %s", e)
        return []


async def ensure_index() -> Dict[str, Any]:
    """Create the Atlas vector search index on the collection if it doesn't exist. The vector size is taken from
    the embedding model itself, so it always matches what upsert() stores."""
    collection = _collection()
    if collection is None:
        return {"created": False, "reason": "MONGODB_URI isn't set"}
    existing = [ix async for ix in collection.list_search_indexes(index_name())]
    if existing:
        return {"created": False, "reason": "already exists", "status": existing[0].get("status")}
    from pymongo.operations import SearchIndexModel

    dimensions = len((await embed(["dimension probe"]))[0])
    definition = {
        "fields": [
            {"type": "vector", "path": "embedding", "numDimensions": dimensions, "similarity": "cosine"},
            *({"type": "filter", "path": p} for p in FILTER_PATHS),
        ]
    }
    # The collection has to exist before Atlas will index it.
    if collection_name() not in await collection.database.list_collection_names():
        await collection.database.create_collection(collection_name())
    await collection.create_search_index(SearchIndexModel(definition=definition, name=index_name(), type="vectorSearch"))
    return {"created": True, "dimensions": dimensions}


async def status() -> Dict[str, Any]:
    """Whether the store is configured and reachable, and what's in it (no credentials)."""
    info: Dict[str, Any] = {
        "enabled": is_enabled(), "database": database_name(), "collection": collection_name(),
        "index": index_name(), "embedding_model": embedding_model(),
    }
    collection = _collection()
    if collection is None:
        return {**info, "connected": False, "detail": "Set MONGODB_URI in .env to turn the vector store on."}
    try:
        await asyncio.wait_for(collection.database.client.admin.command("ping"), timeout=6)
        indexes = [ix async for ix in collection.list_search_indexes(index_name())]
        return {
            **info, "connected": True,
            "documents": await collection.count_documents({}),
            "index_status": indexes[0].get("status") if indexes else "missing",
        }
    except Exception as e:
        return {**info, "connected": False, "detail": f"{type(e).__name__}: {str(e)[:200]}"}
