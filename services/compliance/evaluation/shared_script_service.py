"""Shared documents for script pages.

Every script page (a script_documents row, made blank or from an attached file) gets a document in the Share Document
server, which holds the script's content and the review of it (see that server's script_app_service). This module makes
that document, remembers its id on the page (script_documents.share_document_id) and reads the script back.

SCRIPT_EDITOR=shared turns it on (default "legacy": nothing here does anything, scripts are edited in this portal as
before). A page whose shared document couldn't be made (the server is down, or the creator isn't a user there yet) simply
has none: it keeps working as before and `ensure_shared_document` tries again the next time it is asked for.
Env: SHARE_DOCUMENT_URL (default http://localhost:8006). Both servers must share SCRIPT_SERVICE_SECRET (or SESSION_SECRET when that isn't set): the call is signed with it.
"""

import logging
import os
import re
from typing import Any, Dict, List, Optional, Tuple
from uuid import UUID

import datetime
import secrets

import httpx
import jwt

from postgresql_db.database import execute, fetch_all, fetch_one
from services.shared.auth.service_security_policy import JWT_ALGORITHM, JWT_SECRET

logger = logging.getLogger(__name__)

SOURCE_SERVICE = "social-media"
TIMEOUT_SEC = 20
MAX_BEATS = 80
_MARKS = re.compile(r"\*\*|(?<![\w])_(?=\S)|(?<=\S)_(?![\w])")
_FACTS = re.compile(r"^hook score\s*([^|]*)\|\s*([^|]*)\|\s*(.*)$", re.IGNORECASE)


class SharedScriptError(Exception):
    def __init__(self, message: str, status_code: int = 502):
        super().__init__(message)
        self.status_code = status_code


def enabled() -> bool:
    return os.getenv("SCRIPT_EDITOR", "legacy").strip().lower() in ("shared", "mirror")


def base_url() -> str:
    return os.getenv("SHARE_DOCUMENT_URL", "http://localhost:8006").rstrip("/")


def _service_token(scopes: List[str]) -> str:
    """A 5-minute token naming this service, signed with SCRIPT_SERVICE_SECRET (SESSION_SECRET when that isn't set)."""
    now = datetime.datetime.now(datetime.timezone.utc)
    claims = {"sub": f"service:{SOURCE_SERVICE}", "service": SOURCE_SERVICE, "is_service_token": True, "scopes": scopes,
              "iat": int(now.timestamp()), "exp": int((now + datetime.timedelta(minutes=5)).timestamp()), "jti": secrets.token_urlsafe(12)}
    return jwt.encode(claims, os.getenv("SCRIPT_SERVICE_SECRET") or JWT_SECRET, algorithm=JWT_ALGORITHM)


async def call(method: str, path: str, scopes: List[str], **kwargs: Any) -> Dict[str, Any]:
    """One call to the Share Document server's script API as this service."""
    token = _service_token(scopes)
    try:
        async with httpx.AsyncClient(timeout=TIMEOUT_SEC) as client:
            r = await client.request(method, f"{base_url()}/api/sec-filings/service/script-documents{path}",
                                     headers={"Authorization": f"Bearer {token}"}, **kwargs)
    except httpx.HTTPError as e:
        raise SharedScriptError(f"The Share Document server couldn't be reached: {e}") from e
    if r.status_code >= 400:
        try:
            detail = r.json().get("detail")
        except ValueError:
            detail = None
        raise SharedScriptError(str(detail or r.text[:200]), r.status_code)
    return r.json()


# --------------------------------------------------------------------------- the page's text -> details and beats


def _clean(cell: str) -> str:
    return _MARKS.sub("", cell).strip()


def text_to_beats(text: str) -> Tuple[Dict[str, str], List[Dict[str, str]]]:
    """(details, beats) from a script's text as it is read from an uploaded file: "| a | b |" table rows (Time, Visual /
    footage, Voiceover, On-screen text), a "Storyline:" line, a "Hook score ... | duration | lane" line and a
    "Footage and compliance note:" line. A script with no table gets one beat per line, as voiceover."""
    details: Dict[str, str] = {}
    beats: List[Dict[str, str]] = []
    loose: List[str] = []
    for raw in (text or "").replace("\r\n", "\n").split("\n"):
        line = raw.strip()
        if not line or re.match(r"^--- Page \d+ ---$", line):
            continue
        if re.match(r"^\|.*\|$", line):
            cells = [_clean(c) for c in line.strip("|").split("|")]
            if all(re.fullmatch(r":?-{2,}:?", c) for c in cells) or cells[0].lower() == "time":
                continue
            cells += [""] * (4 - len(cells))
            beats.append({"time": cells[0], "visual": cells[1], "voiceover": cells[2], "onscreen": cells[3]})
            continue
        plain = _clean(re.sub(r"^#{1,3} ", "", line))
        facts = _FACTS.match(plain)
        if plain.lower().startswith("storyline:"):
            details["Storyline"] = plain.split(":", 1)[1].strip()
        elif facts:
            details["Hook score"], details["Duration"], details["Lane"] = (facts.group(1).strip(), facts.group(2).strip(), facts.group(3).strip())
        elif plain.lower().startswith("footage and compliance note"):
            details["Description"] = plain.split(":", 1)[-1].strip()
        else:
            loose.append(plain)
    if not beats:
        beats = [{"time": "", "visual": "", "voiceover": s, "onscreen": ""} for s in loose[1:MAX_BEATS + 1]]  # [0] is the title
    return details, beats[:MAX_BEATS]


# --------------------------------------------------------------------------- making the page's shared document


async def _page(page_id: UUID) -> Optional[Dict[str, Any]]:
    return await fetch_one(
        """SELECT d.id, d.title, d.body_text, d.share_document_id, d.position, u.email, s.title AS set_title
           FROM script_documents d JOIN users u ON u.id = d.created_by LEFT JOIN script_sets s ON s.id = d.set_id
           WHERE d.id = $1 AND d.deleted_at IS NULL""", page_id)


async def delete_shared_documents(page_ids: Optional[List[UUID]] = None, set_id: Optional[UUID] = None) -> None:
    """Delete the shared documents of these pages (or of every page of a set) in one call, as each page's creator, after the
    pages are deleted here. The shared server skips any in review. Never raises: a delete here goes through whether or not the
    shared server is up (the reason is logged), and a page that's gone here is never read from there."""
    if not enabled():
        return
    try:
        rows = await fetch_all(
            """SELECT d.share_document_id, u.email FROM script_documents d JOIN users u ON u.id = d.created_by
               WHERE d.share_document_id IS NOT NULL AND (d.id = ANY($1::uuid[]) OR d.set_id = $2)""",
            list(page_ids or []), set_id)
        by_creator: Dict[str, List[str]] = {}
        for r in rows:
            by_creator.setdefault(r["email"], []).append(r["share_document_id"])
        for email, ids in by_creator.items():
            done = await call("POST", "/delete", ["script_documents:write"], json={"actor_email": email, "document_ids": ids})
            if done.get("skipped"):
                logger.warning("Shared documents kept when deleting scripts: %s", done["skipped"])
    except Exception as e:
        logger.warning("Shared documents not deleted for scripts %s / set %s: %s", page_ids, set_id, e)


async def ensure_shared_document(page_id: UUID) -> Optional[str]:
    """The id of the page's shared document, made now when it has none. None when this is off or it couldn't be made
    (the reason is logged); never raises, so a page is created whether or not the shared server is up."""
    if not enabled():
        return None
    try:
        page = await _page(page_id)
        if not page:
            return None
        if page["share_document_id"]:
            return page["share_document_id"]
        details, beats = text_to_beats(page["body_text"])
        made = await call("POST", "", ["script_documents:write"], json={
            "user_email": page["email"], "title": page["title"], "number": page["position"] or 1, "details": details, "rows": beats,
            "external_ref": {"system": SOURCE_SERVICE, "id": str(page["id"])}})
        # Two requests can race here; the loser leaves a spare shared document behind, which is harmless.
        await execute("UPDATE script_documents SET share_document_id = $2 WHERE id = $1 AND share_document_id IS NULL RETURNING id",
                      page_id, made["document_id"])
        return (await _page(page_id))["share_document_id"]
    except Exception as e:
        logger.warning("No shared document for script %s: %s", page_id, e)
        return None
