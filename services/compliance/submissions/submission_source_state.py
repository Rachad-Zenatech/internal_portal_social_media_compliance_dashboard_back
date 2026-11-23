"""Track the saved source-link inputs of a compliance run, including legacy checks."""
import json
from urllib.parse import urldefrag


def links_key(links):
    return sorted({urldefrag(str(link).strip())[0].rstrip("/") for link in (links or []) if str(link).strip()})


def snapshot(row):
    return links_key(row.get("source_links"))


def sources_changed(row, check):
    if not check:
        return False
    check = json.loads(check) if isinstance(check, str) else check
    checked = check.get("checked_source_links")
    if checked is None:
        checked = [c.get("reference") or c.get("url") for c in check.get("source_checks", [])
                   if c.get("where") == "source_links" and (c.get("reference") or c.get("url"))]
    return snapshot(row) != links_key(checked)
