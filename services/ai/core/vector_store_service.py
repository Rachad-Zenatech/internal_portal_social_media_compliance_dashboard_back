"""Vector store: AWS OpenSearch vector search for reference material the content analysis can draw on
(brand voice, company facts, platform norms, audience profiles and, later, past review decisions).

Uses OPENSEARCH_HOST and OPENSEARCH_INDEX (default `compliance-references`). Gemini or Bedrock supplies real embeddings
in provider/model/dimension-specific fields, so incompatible vectors cannot be mixed.

Off until OPENSEARCH_HOST is set: is_enabled() is False, search() returns nothing and writes are skipped, so nothing
that uses it can fail because the vector store isn't configured.
"""

import asyncio
import hashlib
import logging
import os
import re
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, Iterable, List, Optional, Sequence, Union

from services.ai.core import opensearch_client as os_client
from services.ai.observability.ai_observability import span

logger = logging.getLogger(__name__)

ALL = "*"
EMBED_BATCH = 100  # texts per embedding request
FILTER_PATHS = ["kind", "brands", "audiences", "platforms"]
# Kinds that the assistant never quotes, so changing them doesn't drop its cached answers.
NOT_REFERENCE_KINDS = {"REVIEW_DECISION", "X_CONVERSATION"}
_index_ready: Optional[str] = None


def _setting(name: str, default: str) -> str:
    return os.getenv(name, default).strip() or default


def database_name() -> str:
    return _setting("VECTOR_MONGO_DB", "zenatech_ai")


def collection_name() -> str:
    return _setting("VECTOR_MONGO_COLLECTION", "social_media_compliance_context")


def index_name() -> str:
    return os_client.opensearch_index()


def embedding_model() -> str:
    from services.ai.core.embedding_service import model_id
    return model_id()


def is_enabled() -> bool:
    return os_client.is_opensearch_enabled()


def mongo_enabled() -> bool:
    return bool(os.getenv("MONGODB_URI", "").strip())


_client = None


def named_collection(name: str):
    """Another collection in the MongoDB database, over the same connection, or None when MONGODB_URI isn't set."""
    global _client
    if not mongo_enabled():
        return None
    if _client is None:
        import motor.motor_asyncio  # imported lazily: only needed once MongoDB collections are configured

        kwargs = {"serverSelectionTimeoutMS": 5000}
        try:
            import certifi
            kwargs["tlsCAFile"] = certifi.where()
        except ImportError:
            pass
        _client = motor.motor_asyncio.AsyncIOMotorClient(os.environ["MONGODB_URI"], **kwargs)
    return _client.get_database(database_name()).get_collection(name)


def _embedding_path():
    from services.ai.core.embedding_service import vector_path
    return vector_path()


# Once Gemini says the embedding quota is used up, embedding calls are skipped until it should be back, instead of
# every search and index run asking again (and waiting on retries) only to be refused.
EMBED_DAILY_QUOTA_PAUSE_SECONDS = float(os.getenv("EMBEDDING_DAILY_QUOTA_PAUSE_SECONDS", "1800"))
_embed_paused_until = 0.0
_embed_pause_reason = "Embedding quota used up"
_embed_paused_model = None


class EmbeddingQuotaExhausted(RuntimeError):
    """The embedding quota is used up; raised without calling Gemini while the pause lasts."""


def check_embedding_quota() -> None:
    """Raise EmbeddingQuotaExhausted while embeddings are paused for the quota."""
    left = _embed_paused_until - time.time()
    if left > 0 and _embed_paused_model in (None, embedding_model()):
        raise EmbeddingQuotaExhausted(f"{_embed_pause_reason}; embeddings paused for another {left:.0f}s")


def embedding_pause_status() -> Optional[tuple[str, int]]:
    """Return (reason, seconds_remaining) when embeddings are paused, else None."""
    left = _embed_paused_until - time.time()
    return (_embed_pause_reason, int(left)) if left > 0 and _embed_paused_model in (None, embedding_model()) else None


def note_embedding_error(error: Exception) -> None:
    """Pause embeddings when the error is Gemini's quota refusal: for as long as it says, and for a daily limit at
    least EMBEDDING_DAILY_QUOTA_PAUSE_SECONDS (it won't come back within minutes)."""
    global _embed_paused_until, _embed_pause_reason, _embed_paused_model
    if _embed_paused_model != embedding_model():
        _embed_paused_until = 0
    text = str(error)
    if "AccessDeniedException" in text or getattr(error, "code", None) in (401, 403):
        _embed_paused_model = embedding_model()
        _embed_pause_reason = "Embedding access denied"
        _embed_paused_until = max(_embed_paused_until, time.time() + 3600)
        logger.warning("Embedding access denied; pausing calls for 3600s. Check the embedding provider's permissions.")
        return
    if not any(code in text for code in ("429", "RESOURCE_EXHAUSTED", "ThrottlingException", "ServiceQuotaExceededException")):
        return
    m = re.search(r"'retryDelay': '([0-9.]+)s'", text) or re.search(r"retry in ([0-9.]+)s", text)
    wait = float(m.group(1)) if m else 60.0
    if "perday" in text.lower().replace(" ", "").replace("_", ""):
        wait = max(wait, EMBED_DAILY_QUOTA_PAUSE_SECONDS)
    _embed_pause_reason = "Embedding quota used up"
    _embed_paused_model = embedding_model()
    # Wall-clock time, not monotonic: Docker's VM is suspended while the computer sleeps, and a monotonic clock would
    # not count those hours, so a pause until the daily quota resets would outlast the reset.
    _embed_paused_until = max(_embed_paused_until, time.time() + wait)
    logger.warning("Embedding quota reached; skipping embeddings for %.0fs", wait)


async def embed(texts: List[str], *, task_type="RETRIEVAL_DOCUMENT", background=False) -> List[List[float]]:
    """Return real vectors or fail explicitly; placeholders must never count as retrieval."""
    if not texts:
        return []
    check_embedding_quota()
    try:
        async with span("embedding", "vector_store.embed", model=embedding_model(), texts=len(texts)):
            from services.ai.core.embedding_service import embed as provider_embed
            return await provider_embed(texts, task_type=task_type, background=background)
    except Exception as error:
        note_embedding_error(error)
        raise


def _scope(values: Optional[Iterable[str]]) -> List[str]:
    """An empty scope means every value."""
    cleaned = sorted({v for v in (values or []) if v})
    return cleaned or [ALL]


def _hash(text: str, title: str) -> str:
    return hashlib.sha256(f"real-vectors-2:{embedding_model()}:{_embedding_path()}\n{title}\n{text}".encode("utf-8")).hexdigest()


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


async def _find_existing_by_doc_ids(doc_ids: List[str]) -> Dict[str, Dict[str, Any]]:
    if not doc_ids:
        return {}
    resp = await os_client.request_json(
        "POST",
        f"/{index_name()}/_search",
        json_body={
            "size": min(max(len(doc_ids), 1), 10000),
            "_source": ["doc_id", "content_hash"],
            "query": {"terms": {"doc_id": doc_ids}},
        },
    )
    out: Dict[str, Dict[str, Any]] = {}
    for hit in resp.get("hits", {}).get("hits", []):
        src = hit.get("_source") or {}
        did = src.get("doc_id")
        if did:
            out[did] = {"_id": hit["_id"], "content_hash": src.get("content_hash")}
    return out


async def upsert(docs: List[VectorDoc]) -> Dict[str, int]:
    """Checkpoint small batches so a quota pause preserves already indexed material."""
    counts = {"embedded": 0, "unchanged": 0}
    for start in range(0, len(docs), 20):
        result = await _upsert_batch(docs[start:start + 20])
        for name in counts:
            counts[name] += result[name]
    return counts


async def _upsert_batch(docs: List[VectorDoc]) -> Dict[str, int]:
    """Add or update documents in OpenSearch. Only new or changed text is embedded; scope and meta are refreshed."""
    if not is_enabled() or not docs:
        return {"embedded": 0, "unchanged": 0}
    await ensure_index()
    idx = index_name()
    hashes = {d.doc_id: _hash(d.text, d.title) for d in docs}
    existing = await _find_existing_by_doc_ids(list(hashes))
    changed = [d for d in docs if existing.get(d.doc_id, {}).get("content_hash") != hashes[d.doc_id]]
    vectors = dict(zip(
        (d.doc_id for d in changed),
        await embed([f"{d.title}\n{d.text}".strip() for d in changed], background=True) if changed else [],
    ))
    now = datetime.now(timezone.utc).isoformat()
    ops: List[Dict[str, Any]] = []
    for d in docs:
        fields: Dict[str, Any] = {
            "doc_id": d.doc_id, "kind": d.kind, "source_id": d.source_id, "title": d.title, "text": d.text,
            "brands": _scope(d.brands), "audiences": _scope(d.audiences), "platforms": _scope(d.platforms),
            "meta": d.meta, "updated_at": now,
        }
        if d.doc_id in vectors:
            fields.update(content_hash=hashes[d.doc_id], embedding_model=embedding_model(), **{_embedding_path(): vectors[d.doc_id]})
        prior = existing.get(d.doc_id)
        if prior:
            ops.append({"update": {"_index": idx, "_id": prior["_id"]}})
            ops.append({"doc": fields})
        else:
            ops.append({"index": {"_index": idx}})
            ops.append(fields)
    await os_client.bulk(ops)
    if any(d.kind not in NOT_REFERENCE_KINDS for d in changed):
        await _reference_changed()
    return {"embedded": len(changed), "unchanged": len(docs) - len(changed)}


async def delete(doc_ids: List[str]) -> int:
    if not is_enabled() or not doc_ids:
        return 0
    existing = await _find_existing_by_doc_ids(doc_ids)
    if not existing:
        return 0
    idx = index_name()
    ops = [{"delete": {"_index": idx, "_id": row["_id"]}} for row in existing.values()]
    await os_client.bulk(ops)
    if any(i.split(":", 1)[0] not in NOT_REFERENCE_KINDS for i in existing):
        await _reference_changed()
    return len(existing)


async def delete_kind_except(kind: str, keep_doc_ids: List[str]) -> int:
    """Remove every document of this kind that isn't in keep_doc_ids, so the index for a kind mirrors its source."""
    if not is_enabled():
        return 0
    head = await os_client.request("HEAD", f"/{index_name()}")
    if head.status_code == 404:
        return 0
    query: Dict[str, Any] = {"bool": {"filter": [{"term": {"kind": kind}}]}}
    if keep_doc_ids:
        query["bool"]["must_not"] = [{"terms": {"doc_id": keep_doc_ids}}]
    resp = await os_client.request_json(
        "POST",
        f"/{index_name()}/_search",
        json_body={"size": 10000, "_source": False, "query": query},
    )
    hits = resp.get("hits", {}).get("hits", [])
    if not hits:
        return 0
    idx = index_name()
    ops = [{"delete": {"_index": idx, "_id": h["_id"]}} for h in hits]
    await os_client.bulk(ops)
    if kind not in NOT_REFERENCE_KINDS:
        await _reference_changed()
    return len(hits)


async def _reference_changed() -> None:
    """Assistant answers may quote reference material, so cached ones are dropped when it changes (past review
    decisions aren't shown to the assistant, so those don't count)."""
    from services.ai.core import semantic_cache
    try:
        await semantic_cache.clear("CHAT")
    except Exception as e:
        logger.warning("Couldn't clear cached assistant answers after a reference material change: %s", e)


async def search(
    query: str,
    *,
    kinds: Optional[List[str]] = None,
    brand: Optional[Union[str, Sequence[str]]] = None,
    audience: Optional[Union[str, Sequence[str]]] = None,
    platforms: Optional[List[str]] = None,
    limit: int = 5,
    min_score: float = 0.0,
) -> List[Dict[str, Any]]:
    """The passages closest in meaning to the query, limited to those scoped to this brand / audience / platforms
    (or to all of them). Returns [] when the store is off or anything goes wrong, so callers can always go ahead."""
    if not is_enabled() or not query.strip():
        return []
    filters: List[Dict[str, Any]] = []
    if kinds:
        filters.append({"terms": {"kind": kinds}})
    if brand:
        filters.append({"terms": {"brands": [*([brand] if isinstance(brand, str) else brand), ALL]}})
    if audience:
        filters.append({"terms": {"audiences": [*([audience] if isinstance(audience, str) else audience), ALL]}})
    if platforms:
        filters.append({"terms": {"platforms": [*platforms, ALL]}})
    try:
        await ensure_index()
        async with span("retrieval", "vector_store.search", kinds=kinds, limit=limit, min_score=min_score) as s:
            query_vector = (await asyncio.wait_for(embed([query], task_type="RETRIEVAL_QUERY"),
                           timeout=float(os.getenv("VECTOR_QUERY_EMBED_TIMEOUT_SEC", "5"))))[0]
            knn_clause: Dict[str, Any] = {"vector": query_vector, "k": limit}
            if filters:
                knn_clause["filter"] = {"bool": {"filter": filters}}
            resp = await os_client.request_json(
                "POST",
                f"/{index_name()}/_search",
                json_body={
                    "size": limit,
                    "_source": ["doc_id", "kind", "title", "text", "source_id", "meta", "brands"],
                    "query": {"knn": {_embedding_path(): knn_clause}},
                },
            )
            found: List[Dict[str, Any]] = []
            for hit in resp.get("hits", {}).get("hits", []):
                score = float(hit.get("_score") or 0.0)
                if score < min_score:
                    continue
                src = hit.get("_source") or {}
                found.append({
                    "_id": src.get("doc_id") or hit["_id"],
                    "kind": src.get("kind"),
                    "title": src.get("title"),
                    "text": src.get("text"),
                    "source_id": src.get("source_id"),
                    "meta": src.get("meta") or {},
                    "brands": src.get("brands") or [],
                    "score": score,
                })
            s.attributes.update(results=len(found), top_score=round(found[0]["score"], 4) if found else None)
            return found
    except EmbeddingQuotaExhausted as e:
        logger.debug("Vector search skipped while embeddings paused: %s", e)
        return []
    except Exception as e:
        logger.warning("Vector search failed (continuing without it): %s", e)
        return []


async def ensure_index() -> Dict[str, Any]:
    """Create the OpenSearch k-NN index if it doesn't exist. Dimensions match the configured embedding model."""
    global _index_ready
    if not is_enabled():
        return {"created": False, "reason": "OPENSEARCH_HOST isn't set"}
    idx = index_name()
    if _index_ready == idx:
        return {"created": False, "reason": "already exists", "status": "READY"}
    head = await os_client.request("HEAD", f"/{idx}")
    if head.status_code == 200:
        _index_ready = idx
        return {"created": False, "reason": "already exists", "status": "READY"}
    from services.ai.core.embedding_service import dimensions as configured_dimensions
    dims = configured_dimensions()
    body = {
        "settings": {"index": {"knn": True}},
        "mappings": {
            "properties": {
                "doc_id": {"type": "keyword"},
                "kind": {"type": "keyword"},
                "source_id": {"type": "keyword"},
                "title": {"type": "text"},
                "text": {"type": "text"},
                "brands": {"type": "keyword"},
                "audiences": {"type": "keyword"},
                "platforms": {"type": "keyword"},
                "content_hash": {"type": "keyword"},
                "embedding_model": {"type": "keyword"},
                "updated_at": {"type": "date"},
                "meta": {"type": "object", "enabled": False},
                _embedding_path(): {
                    "type": "knn_vector",
                    "dimension": dims,
                    "method": {"name": "hnsw", "engine": "nmslib", "space_type": "cosinesimil"},
                },
            }
        },
    }
    resp = await os_client.request("PUT", f"/{idx}", json_body=body)
    if resp.status_code >= 400 and "resource_already_exists_exception" not in resp.text:
        raise RuntimeError(f"OpenSearch index creation failed ({resp.status_code}): {resp.text[:400]}")
    _index_ready = idx
    return {"created": True, "dimensions": dims, "index": idx}


async def status() -> Dict[str, Any]:
    """Whether OpenSearch is configured and reachable, and what's in the index (no credentials)."""
    info: Dict[str, Any] = {
        "enabled": is_enabled(), "provider": "aws_opensearch", "host": os_client.opensearch_host(),
        "index": index_name(), "embedding_model": embedding_model(),
    }
    if not is_enabled():
        return {**info, "connected": False, "detail": "Set OPENSEARCH_HOST in .env to turn the vector store on."}
    try:
        idx = index_name()
        head = await os_client.request("HEAD", f"/{idx}", timeout=6)
        if head.status_code == 404:
            return {**info, "connected": True, "documents": 0, "index_status": "missing"}
        if head.status_code >= 400:
            return {**info, "connected": False, "detail": f"HTTP {head.status_code}: {head.text[:200]}"}
        count_resp = await os_client.request_json("GET", f"/{idx}/_count", timeout=6)
        return {**info, "connected": True, "documents": int(count_resp.get("count", 0)), "index_status": "READY"}
    except Exception as e:
        return {**info, "connected": False, "detail": f"{type(e).__name__}: {str(e)[:200]}"}
