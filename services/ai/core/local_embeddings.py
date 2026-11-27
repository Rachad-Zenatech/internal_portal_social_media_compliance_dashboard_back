"""Local HuggingFace/PyTorch embedding provider (BAAI/bge-small-en-v1.5) requiring no cloud API permissions."""
import asyncio
import hashlib
import importlib.util
import logging
import os
import threading
from typing import List

logger = logging.getLogger(__name__)

_DEFAULT_MODEL = "BAAI/bge-small-en-v1.5"
_DEFAULT_DIMS = {"bge-small": 384, "bge-base": 768, "bge-large": 1024}
_lock = threading.Lock()
_loaded = None  # (model_name, tokenizer, model)


def model_id() -> str:
    return (os.getenv("LOCAL_EMBEDDING_MODEL") or _DEFAULT_MODEL).strip()


def dimensions() -> int:
    raw = os.getenv("LOCAL_EMBEDDING_DIMENSIONS", "").strip()
    if raw:
        return int(raw)
    name = model_id().lower()
    for prefix, dim in _DEFAULT_DIMS.items():
        if prefix in name:
            return dim
    return 384


def vector_path() -> str:
    identity = f"local-v1:{model_id()}:{dimensions()}"
    return "embedding_" + hashlib.sha256(identity.encode()).hexdigest()[:16]


def is_configured() -> bool:
    return (
        importlib.util.find_spec("transformers") is not None
        and importlib.util.find_spec("torch") is not None
    )


def _load_model():
    global _loaded
    name = model_id()
    if _loaded is not None and _loaded[0] == name:
        return _loaded[1], _loaded[2]
    with _lock:
        if _loaded is not None and _loaded[0] == name:
            return _loaded[1], _loaded[2]
        from transformers import AutoModel, AutoTokenizer

        logger.info("Loading local embedding model: %s", name)
        tokenizer = AutoTokenizer.from_pretrained(name)
        model = AutoModel.from_pretrained(name)
        model.eval()
        _loaded = (name, tokenizer, model)
        return tokenizer, model


def _embed_sync(texts: List[str]) -> List[List[float]]:
    import torch
    import torch.nn.functional as F

    tokenizer, model = _load_model()
    batch_size = max(1, int(os.getenv("LOCAL_EMBEDDING_BATCH_SIZE", "32")))
    out: List[List[float]] = []
    with torch.no_grad():
        for start in range(0, len(texts), batch_size):
            batch = texts[start:start + batch_size]
            encoded = tokenizer(batch, padding=True, truncation=True, max_length=512, return_tensors="pt")
            outputs = model(**encoded)
            # BGE v1.5 uses CLS pooling followed by L2 normalization.
            cls_emb = outputs.last_hidden_state[:, 0]
            normed = F.normalize(cls_emb, p=2, dim=1)
            out.extend(normed.cpu().tolist())
    return out


async def embed(texts: List[str]) -> List[List[float]]:
    if not texts:
        return []
    if any(not isinstance(text, str) or not text.strip() or len(text) > 50000 for text in texts):
        raise ValueError("Embedding input must contain 1 to 50000 characters")
    return await asyncio.to_thread(_embed_sync, list(texts))
