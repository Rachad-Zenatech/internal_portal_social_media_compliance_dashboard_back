"""Share exact-content source verdicts across users, workers, restarts and ordinary reruns."""
import hashlib
import json
import logging

from postgresql_db import database as db
from services.compliance.evaluation.source_verdict_models import _Result

logger = logging.getLogger(__name__)


def identity(source_text, label, title, url, published, models):
    payload = [" ".join(source_text.split()), label, title, url, published, models]
    return hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()


def decode(payload):
    payload = json.loads(payload) if isinstance(payload, str) else payload
    return ([_Result.model_validate(item) for item in payload["claims"]], payload.get("model"))


async def get(key):
    try:
        row = await db.fetch_one("SELECT result FROM source_claim_comparisons WHERE comparison_key=$1", key)
        return decode(row["result"]) if row else None
    except Exception as error:
        logger.warning("Shared source comparison cache unavailable: %s", type(error).__name__)
        return None


async def put(key, answer):
    payload = json.dumps({"claims": [c.model_dump() for c in answer[0]], "model": answer[1]})
    try:
        row = await db.execute(
            "INSERT INTO source_claim_comparisons (comparison_key,result) VALUES ($1,$2::jsonb) "
            "ON CONFLICT (comparison_key) DO UPDATE SET comparison_key=EXCLUDED.comparison_key RETURNING result",
            key, payload)
        return decode(row["result"])  # First complete answer wins concurrent comparisons.
    except Exception as error:
        logger.warning("Could not share source comparison result: %s", type(error).__name__)
        return answer
