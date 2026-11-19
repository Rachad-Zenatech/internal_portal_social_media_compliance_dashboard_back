"""Custom default script template uploaded from the Social Media portal.

Replaces the built-in 4-column Reels script template for new blank scripts and added pages,
and passes the custom table headers/rows to Share Document when creating the shared document.
"""

import json
import re
from typing import Any, Dict, List, Optional, Tuple
from uuid import UUID

from fastapi import HTTPException

from postgresql_db.database import execute, fetch_one
from services.compliance.evaluation import script_batch_service as batches

DEFAULT_HEADERS = ["Time", "Visual / footage", "Voiceover", "On-screen text"]
_MARKS = re.compile(r"\*\*|(?<![\w])_(?=\S)|(?<=\S)_(?![\w])")
_FACTS = re.compile(r"^hook score\s*([^|]*)\|\s*([^|]*)\|\s*(.*)$", re.IGNORECASE)

_ENSURE_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS script_default_template (
    id INT PRIMARY KEY DEFAULT 1 CHECK (id = 1),
    filename TEXT NOT NULL,
    headers JSONB NOT NULL,
    table_rows JSONB NOT NULL,
    details JSONB NOT NULL,
    body_html TEXT NOT NULL,
    body_text TEXT NOT NULL,
    updated_by UUID,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
)
"""


def _esc(s: str) -> str:
    return (s or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _parse_json(val: Any, fallback: Any) -> Any:
    if isinstance(val, str):
        try:
            return json.loads(val)
        except ValueError:
            return fallback
    return val if val is not None else fallback


def parse_table_from_text(text: Optional[str]) -> Tuple[Optional[List[str]], List[List[str]], Dict[str, str]]:
    """Extract (headers, data_rows, details) from light-mark text (| col1 | col2 |)."""
    details: Dict[str, str] = {}
    table: List[List[str]] = []
    for raw in (text or "").splitlines():
        line = _MARKS.sub("", raw).strip()
        if not line:
            continue
        if line.startswith("|") and line.endswith("|") and len(line) > 2:
            cells = [c.strip() for c in line.strip("|").split("|")]
            if any(cells) and not all(re.fullmatch(r"[-:\s]*", c) for c in cells):
                table.append(cells)
        elif line.lower().startswith("storyline:"):
            details["Storyline"] = line.split(":", 1)[1].strip()
        elif line.lower().startswith("footage and compliance note"):
            details["Description"] = line.split(":", 1)[1].strip() if ":" in line else ""
        elif _FACTS.match(line):
            m = _FACTS.match(line)
            if m:
                details["Hook score"] = m.group(1).strip()
                details["Duration"] = m.group(2).strip()
                details["Lane"] = m.group(3).strip()
    if not table:
        return None, [], details
    headers = [c or f"Column {i + 1}" for i, c in enumerate(table[0])]
    width = len(headers)
    rows = [[*r, *([""] * width)][:width] for r in table[1:81]]
    return headers, rows, details


def build_template_html(n: int, title: str, headers: List[str], rows: List[List[str]], details: Dict[str, str]) -> str:
    """Render a script page HTML from template headers, rows, and details."""
    head = "<tr>" + "".join(f"<th><p>{_esc(h)}</p></th>" for h in headers) + "</tr>"
    effective_rows = rows or [[""] * len(headers) for _ in range(5)]
    body = "".join(
        "<tr>" + "".join(f"<td><p>{_esc(c)}</p></td>" for c in r[:len(headers)]) + "</tr>"
        for r in effective_rows
    )
    storyline = _esc(details.get("Storyline", ""))
    facts = _esc(" | ".join(x for x in (
        f"Hook score {details.get('Hook score', '')}".strip(),
        details.get("Duration") or "about 35 seconds",
        details.get("Lane") or "Green lane",
    ) if x))
    return (
        f"<h2>{n}. {_esc(title)}</h2>"
        f"<p><strong>Storyline:</strong> {storyline}</p>"
        f"<p>{facts}</p>"
        f"<table><tbody>{head}{body}</tbody></table><p></p>"
    )


async def _ensure_table() -> None:
    await execute(_ENSURE_TABLE_SQL)


async def get_default_template() -> Optional[Dict[str, Any]]:
    await _ensure_table()
    row = await fetch_one("SELECT * FROM script_default_template WHERE id = 1")
    if not row:
        return None
    return {
        "filename": row["filename"],
        "headers": _parse_json(row["headers"], DEFAULT_HEADERS),
        "table_rows": _parse_json(row["table_rows"], []),
        "details": _parse_json(row["details"], {}),
        "body_html": row["body_html"],
        "body_text": row["body_text"],
        "updated_at": row["updated_at"].isoformat() if row["updated_at"] else None,
    }


async def get_default_template_summary() -> Dict[str, Any]:
    tpl = await get_default_template()
    if not tpl:
        return {"is_custom": False, "filename": None, "headers": list(DEFAULT_HEADERS), "row_count": 5, "updated_at": None}
    return {
        "is_custom": True,
        "filename": tpl["filename"],
        "headers": tpl["headers"],
        "row_count": len(tpl["table_rows"]) or 5,
        "updated_at": tpl["updated_at"],
    }


async def save_default_template(user_id: UUID, filename: str, data: bytes) -> Dict[str, Any]:
    text = await batches.read_upload(filename, data)
    if not (text or "").strip():
        raise HTTPException(status_code=422, detail="The uploaded template file is empty")
    headers, rows, details = parse_table_from_text(text)
    active_headers = headers or list(DEFAULT_HEADERS)
    active_rows = rows or [[""] * len(active_headers) for _ in range(5)]
    html = build_template_html(1, "Untitled script", active_headers, active_rows, details)
    await _ensure_table()
    await execute(
        """
        INSERT INTO script_default_template (id, filename, headers, table_rows, details, body_html, body_text, updated_by, updated_at)
        VALUES (1, $1, $2::jsonb, $3::jsonb, $4::jsonb, $5, $6, $7, NOW())
        ON CONFLICT (id) DO UPDATE SET
            filename = EXCLUDED.filename, headers = EXCLUDED.headers, table_rows = EXCLUDED.table_rows,
            details = EXCLUDED.details, body_html = EXCLUDED.body_html, body_text = EXCLUDED.body_text,
            updated_by = EXCLUDED.updated_by, updated_at = NOW()
        RETURNING id
        """,
        filename[:300],
        json.dumps(active_headers),
        json.dumps(active_rows),
        json.dumps(details),
        html,
        text,
        user_id,
    )
    return await get_default_template_summary()


async def reset_default_template() -> Dict[str, Any]:
    await _ensure_table()
    await execute("DELETE FROM script_default_template WHERE id = 1")
    return await get_default_template_summary()


async def initial_content_for(n: int, title: str, fallback_html: str) -> Tuple[str, str]:
    """Return (body_html, body_text) using the uploaded default template if one is active."""
    tpl = await get_default_template()
    if not tpl:
        return fallback_html, ""
    return build_template_html(n, title, tpl["headers"], tpl["table_rows"], tpl["details"]), tpl["body_text"]
