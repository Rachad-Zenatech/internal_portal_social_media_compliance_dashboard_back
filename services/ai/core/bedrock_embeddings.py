"""Real Titan embeddings, isolated from chat inference profiles and old vector spaces."""
import asyncio
import hashlib
import json
import math
import os

from services.ai.core.bedrock_service import get_bedrock_client


def model_id():
    return os.getenv("BEDROCK_EMBEDDING_MODEL", "amazon.titan-embed-text-v2:0").strip()


def dimensions():
    value = int(os.getenv("BEDROCK_EMBEDDING_DIMENSIONS", "1024"))
    if value not in {256, 512, 1024}:
        raise ValueError("BEDROCK_EMBEDDING_DIMENSIONS must be 256, 512 or 1024")
    return value


def vector_path():
    identity = f"titan-v2:{model_id()}:{dimensions()}"
    return "embedding_" + hashlib.sha256(identity.encode()).hexdigest()[:16]


def _embed_one(text):
    response = get_bedrock_client().get_boto_client().invoke_model(
        modelId=model_id(), contentType="application/json", accept="application/json",
        body=json.dumps({"inputText": text, "dimensions": dimensions(), "normalize": True}),
    )
    body = response["body"]
    try:
        vector = json.loads(body.read()).get("embedding", [])
    finally:
        body.close()
    if (len(vector) != dimensions() or not all(isinstance(x, (int, float)) and math.isfinite(x) for x in vector)
            or not any(vector)):
        raise ValueError("Bedrock returned an invalid embedding; refusing to store or search it")
    return vector


async def embed(texts):
    # Bound concurrency and payloads. Never truncate silently or substitute fake vectors.
    vectors = []
    for start in range(0, len(texts), 4):
        batch = texts[start:start + 4]
        if any(not text.strip() or len(text) > 50000 for text in batch):
            raise ValueError("Embedding input must contain 1 to 50000 characters")
        vectors.extend(await asyncio.gather(*(asyncio.to_thread(_embed_one, text) for text in batch)))
    return vectors
