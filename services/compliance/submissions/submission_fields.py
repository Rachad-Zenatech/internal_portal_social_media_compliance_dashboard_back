"""Field shaping for submission summaries and draft type changes."""
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
        if payload["content_type"] == "VIDEO" and row["current_version"] == 0 and not row.get("script_required"):
            fields["script_required"] = True
    return fields


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
