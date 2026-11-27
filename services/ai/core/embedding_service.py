"""Choose an embedding provider independently of the Claude review models."""
import asyncio
import hashlib
import math
import os
import re
import time
from collections import deque

import httpx

from services.ai.core import bedrock_embeddings, local_embeddings

_rate_lock = asyncio.Lock()
_rate_windows = {False: deque(), True: deque()}


class EmbeddingCapacityBusy(RuntimeError):
    """Interactive retrieval must not queue behind a background index rebuild."""


def _capacity(background):
    per_minute = max(1, int(os.getenv("GEMINI_EMBEDDING_REQUESTS_PER_MINUTE", "60")))
    background_limit = max(1, per_minute // 3)
    return background_limit if background else max(1, per_minute - background_limit)


async def _pace(count, *, background=False):
    """Reserve separate rolling capacity; only background indexing is allowed to wait."""
    limit = _capacity(background)
    if count > limit:
        raise ValueError("Embedding batch exceeds its reserved per-minute capacity")
    while True:
        async with _rate_lock:
            now = time.monotonic()
            window = _rate_windows[background]
            while window and window[0][0] <= now - 60:
                window.popleft()
            if sum(size for _, size in window) + count <= limit:
                window.append((now, count))
                return
            delay = max(.01, window[0][0] + 60 - now)
        if not background:
            raise EmbeddingCapacityBusy("Interactive embedding capacity is busy; continue using available references.")
        await asyncio.sleep(delay)


def provider():
    value = os.getenv("EMBEDDING_PROVIDER", "local").strip().lower()
    if value not in {"local", "bedrock", "gemini"}:
        raise ValueError("EMBEDDING_PROVIDER must be local, bedrock or gemini")
    return value


def model_id():
    p = provider()
    if p == "local":
        return local_embeddings.model_id()
    if p == "bedrock":
        return bedrock_embeddings.model_id()
    value = os.getenv("GEMINI_EMBEDDING_MODEL") or os.getenv("VECTOR_EMBEDDING_MODEL", "")
    return value.strip().removeprefix("models/") if value.strip().startswith(("gemini-embedding-", "models/gemini-embedding-")) else "gemini-embedding-2"


def dimensions():
    p = provider()
    if p == "local":
        return local_embeddings.dimensions()
    if p == "bedrock":
        return bedrock_embeddings.dimensions()
    value = int(os.getenv("GEMINI_EMBEDDING_DIMENSIONS", "768"))
    if value not in {768, 1536, 3072}:
        raise ValueError("GEMINI_EMBEDDING_DIMENSIONS must be 768, 1536 or 3072")
    return value


def vector_path():
    p = provider()
    if p == "local":
        return local_embeddings.vector_path()
    if p == "bedrock":
        return bedrock_embeddings.vector_path()
    identity = f"gemini-real-v1:{model_id()}:{dimensions()}"
    return "embedding_" + hashlib.sha256(identity.encode()).hexdigest()[:16]


def is_configured():
    p = provider()
    if p == "local":
        return local_embeddings.is_configured()
    if p == "gemini":
        return bool(os.getenv("GEMINI_API_KEY", "").strip())
    from services.ai.core.bedrock_service import is_bedrock_enabled
    return is_bedrock_enabled()


class GeminiEmbeddingError(RuntimeError):
    def __init__(self, code, message):
        super().__init__(f"Gemini embedding error {code}: {message}")
        self.code = code


def _vectors(result, expected):
    rows = result.get("embeddings", [])
    if len(rows) != expected:
        raise ValueError("Gemini returned an incomplete embedding batch")
    out = []
    for row in rows:
        values = row.get("values", [])
        if (len(values) != dimensions() or not all(isinstance(v, (int, float)) and math.isfinite(v) for v in values)
                or not any(values)):
            raise ValueError("Gemini returned an invalid embedding; refusing to store or search it")
        norm = math.sqrt(sum(v * v for v in values))
        out.append([v / norm for v in values])
    return out


async def _request(client, model, requests, key, *, background=False):
    for attempt in range(2):
        await _pace(len(requests), background=background)
        response = await client.post(
            f"https://generativelanguage.googleapis.com/v1beta/{model}:batchEmbedContents",
            headers={"x-goog-api-key": key}, json={"requests": requests},
        )
        if response.is_success:
            return _vectors(response.json(), len(requests))
        try:
            message = str(response.json().get("error", {}).get("message", "Request failed"))
        except ValueError:
            message = "Request failed"
        minute_limit = "perminute" in message.lower().replace("_", "").replace(" ", "")
        delay = re.search(r"retry in ([0-9.]+)s", message, re.IGNORECASE)
        if background and response.status_code == 429 and not attempt and minute_limit and delay and float(delay[1]) <= 60:
            await asyncio.sleep(float(delay[1]) + 1)
            continue
        raise GeminiEmbeddingError(response.status_code, message.replace(key, "[redacted]")[:1000])


async def embed(texts, *, task_type="RETRIEVAL_DOCUMENT", background=False):
    if not texts:
        return []
    p = provider()
    if p == "local":
        return await local_embeddings.embed(texts)
    if p == "bedrock":
        return await bedrock_embeddings.embed(texts)
    key = os.getenv("GEMINI_API_KEY", "").strip()
    if not key:
        raise ValueError("GEMINI_API_KEY is required for Gemini embeddings")
    if any(not isinstance(text, str) or not text.strip() or len(text) > 50000 for text in texts):
        raise ValueError("Embedding input must contain 1 to 50000 characters")
    model = "models/" + model_id()
    vectors = []
    batch_size = max(1, min(int(os.getenv("GEMINI_EMBEDDING_BATCH_SIZE", "20")), 32, _capacity(background)))
    async with httpx.AsyncClient(timeout=float(os.getenv("GEMINI_EMBEDDING_TIMEOUT_SEC", "30"))) as client:
        for start in range(0, len(texts), batch_size):
            batch = texts[start:start + batch_size]
            requests = [{"model": model, "content": {"parts": [{"text": text}]},
                         "taskType": task_type, "outputDimensionality": dimensions()} for text in batch]
            vectors.extend(await _request(client, model, requests, key, background=background))
    return vectors
