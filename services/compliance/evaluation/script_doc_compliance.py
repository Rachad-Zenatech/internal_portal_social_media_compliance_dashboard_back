"""Keep the last per-script compliance result and identify changes to its inputs."""
import json
from datetime import datetime, timezone

from postgresql_db.database import execute
from services.compliance.evaluation.script_doc_sources import sources_for
from services.compliance.media.script_sources import extract

CONTEXT_KEYS = ("platforms", "rule_sets", "brand_codes", "audiences", "region")


def inputs(doc: dict, text: str | None = None) -> dict:
    details = doc.get("details") or {}
    if isinstance(details, str):
        details = json.loads(details)
    body, _ = extract(doc.get("body_text", "") if text is None else text)
    return {"text": " ".join(body.split()), "sources": sources_for(doc).strip(),
            "context": {"title": doc["title"], **{key: details.get(key) for key in CONTEXT_KEYS}}}


def state(doc: dict) -> dict:
    result = doc.get("latest_check")
    if isinstance(result, str):
        result = json.loads(result)
    return {"latest_check": result, "compliance_stale": bool(result and result.get("checked_script_inputs") != inputs(doc))}


async def save(doc: dict, result: dict, text: str) -> dict:
    result = {**result, "checked_at": datetime.now(timezone.utc).isoformat(),
              "checked_script_sources": sources_for(doc), "checked_script_inputs": inputs(doc, text)}
    # Only the check column changes: concurrent edits keep their text and sources,
    # and comparison with the saved inputs will mark this result outdated.
    await execute("UPDATE script_documents SET latest_check = $2::jsonb WHERE id = $1 RETURNING id",
                  doc["id"], json.dumps(result))
    return result
