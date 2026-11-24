"""Field shaping for submission summaries and draft type changes."""
import json

from fastapi import HTTPException

from services.compliance.media import document_layout


def summary_fields(row):
    fields = {k: v for k, v in row.items()
              if not k.startswith(("acc_", "cov_"))
              and k not in ("platforms", "accounts_json", "due_at", "due_kind", "rule_sets", "platform_captions")}
    if fields.get("script_excerpt"):
        fields["script_excerpt"] = " ".join(document_layout.plain_text(fields["script_excerpt"]).split())
    return fields


def editable_type_fields(row, payload):
    fields = {}
    if "title" in payload and payload["title"] is not None:
        fields["title"] = payload["title"].strip()
    if payload.get("content_type"):
        fields["content_type"] = payload["content_type"]
        if payload["content_type"] == "VIDEO" and row.get("content_type") != "VIDEO" and row["current_version"] == 0:
            fields["script_required"] = True
    fields.update(script_mode_fields(row, payload))
    return fields


def script_mode_fields(row, payload):
    required = payload.get("script_required")
    if required is None:
        return {}
    content_type = payload.get("content_type") or row["content_type"]
    if content_type != "VIDEO":
        return {}
    if required == bool(row.get("script_required")) and row["content_type"] == "VIDEO":
        return {}
    send = row.get("script_send") or {}
    if isinstance(send, str):
        send = json.loads(send)
    if (row.get("current_version", 0) != 0 or row.get("status") != "DRAFT"
            or row.get("script_status") not in (None, "DRAFT") or send.get("status") == "PROCESSING"):
        raise HTTPException(status_code=409, detail="Choose whether to skip script review before sending the script or post for review.")
    return {"script_required": required}


async def lock_script_mode(conn, submission_id, payload):
    """Do not let a racing autosave bypass a script that has just been sent."""
    if payload.get("script_required") is not None:
        row = await conn.fetchrow("SELECT * FROM content_submissions WHERE id = $1 FOR UPDATE", submission_id)
        script_mode_fields(row, payload)


def apply_clear_fields(fields, clear_fields):
    """Clear scalar selections together with their plural representations."""
    for name in clear_fields:
        fields[name] = None
        if name == "account_id":
            fields["account_ids"] = []
        if name == "audience":
            fields["audiences"] = []
        if name == "brand_code":
            fields["brand_codes"] = []
