"""Live editing of script documents.

Each open editor holds a WebSocket. The script's text and its notes are Yjs documents on the browsers; this module only
stores and relays their binary updates, so it needs no Yjs library:

  * every update is saved (script_document_updates) and sent to everyone else who has the script open, so a person who
    opens it later gets the whole history of updates and rebuilds the same document;
  * cursors and names (Yjs "awareness") are relayed and never saved;
  * review state (check-ins, status) is pushed to everyone as it changes.

The app runs on several workers, so what one worker receives is also sent through PostgreSQL NOTIFY for the others to
deliver to their own connections. Nothing polls: each connection waits for the next message.

Permissions are enforced here: only someone who may edit can change the text; notes need the right to note.
"""

import asyncio
import base64
import json
import logging
import os
import uuid
from typing import Any, Dict, Optional
from uuid import UUID

import asyncpg
from fastapi import WebSocket, WebSocketDisconnect

from postgresql_db.database import execute, fetch_all, fetch_one

logger = logging.getLogger(__name__)

CHANNEL = "script_doc_rt"
WORKER = uuid.uuid4().hex
MAX_MESSAGE_BYTES = 2 * 1024 * 1024
PING_SECONDS = 25.0
NOTIFY_LIMIT = 7000

_conn_ids = iter(range(1, 1 << 62))


class Conn:
    def __init__(self, ws: WebSocket, user_id: UUID, name: str, doc_id: UUID):
        self.id = next(_conn_ids)
        self.ws, self.user_id, self.name, self.doc_id = ws, user_id, name, doc_id
        self.can_edit = False
        self.can_note = False
        self.seeding = False
        self.queue: "asyncio.Queue[Optional[Dict[str, Any]]]" = asyncio.Queue()


_rooms: Dict[UUID, Dict[int, Conn]] = {}


def _deliver_local(doc_id: UUID, msg: Dict[str, Any], exclude: Optional[int] = None) -> None:
    for c in list(_rooms.get(doc_id, {}).values()):
        if c.id != exclude:
            c.queue.put_nowait(msg)


async def _notify(payload: Dict[str, Any]) -> None:
    raw = json.dumps(payload)
    if len(raw) > NOTIFY_LIMIT:
        logger.debug("Script document message too large to relay across workers (%s bytes)", len(raw))
        return
    try:
        await execute("SELECT pg_notify($1, $2)", CHANNEL, raw)
    except Exception as e:
        logger.debug("Script document NOTIFY failed: %s", e)


async def publish(doc_id: UUID, msg: Dict[str, Any], exclude: Optional[int] = None) -> None:
    """Send a message to everyone who has the script open, on every worker."""
    _deliver_local(doc_id, msg, exclude)
    await _notify({"w": WORKER, "d": str(doc_id), "m": msg})


async def _on_remote(raw: str) -> None:
    try:
        data = json.loads(raw)
        if data.get("w") == WORKER:
            return
        doc_id = UUID(data["d"])
        if doc_id not in _rooms:
            return
        msg = data["m"]
        if msg.get("type") == "update_ref":
            row = await fetch_one("SELECT payload FROM script_document_updates WHERE id = $1", msg["row"])
            if not row:
                return
            msg = {"type": "update", "ch": msg["ch"], "d": base64.b64encode(row["payload"]).decode(), "from": msg["from"]}
        _deliver_local(doc_id, msg)
    except Exception:
        logger.warning("Could not relay a script document message", exc_info=True)


async def start_listener() -> None:
    """Background task: receives other workers' messages (reconnects when the connection drops)."""
    db_url = os.environ.get("DATABASE_URL")
    if not db_url:
        logger.warning("DATABASE_URL not configured. Script document relay between workers disabled.")
        return
    ssl_mode = os.getenv("DATABASE_SSL", "false").lower()
    ssl_opt = "require" if (ssl_mode in ("true", "require", "1") or "rds.amazonaws.com" in db_url or "supabase.com" in db_url) else False

    def handler(connection, pid, channel, payload):
        asyncio.get_running_loop().create_task(_on_remote(payload))

    while True:
        conn = None
        try:
            conn = await asyncpg.connect(db_url, ssl=ssl_opt)
            await conn.add_listener(CHANNEL, handler)
            while not conn.is_closed():
                await asyncio.sleep(15)
        except asyncio.CancelledError:
            if conn and not conn.is_closed():
                try:
                    await conn.remove_listener(CHANNEL, handler)
                    await conn.close()
                except Exception:
                    pass
            break
        except Exception as e:
            logger.warning("Script document relay disconnected (%s), reconnecting in 5s...", e)
            if conn and not conn.is_closed():
                try:
                    await conn.close()
                except Exception:
                    pass
            await asyncio.sleep(5)


async def _refresh_access(c: Conn) -> None:
    from services.compliance.evaluation import script_doc_service as svc
    try:
        _doc, _reviewers, access = await svc.get_access(c.doc_id, c.user_id)
        c.can_edit, c.can_note = access.can_edit, access.can_note
    except Exception:
        c.can_edit = c.can_note = False


async def _send_loop(c: Conn) -> None:
    """Sends what's queued for this connection, plus a keep-alive ping (not a poll: nothing is read from the database)."""
    while True:
        try:
            msg = await asyncio.wait_for(c.queue.get(), timeout=PING_SECONDS)
        except asyncio.TimeoutError:
            msg = {"type": "ping"}
        if msg is None:
            return
        if msg.get("type") == "state":
            await _refresh_access(c)
            msg = {**msg, "you": {"can_edit": c.can_edit, "can_note": c.can_note}}
        await c.ws.send_text(json.dumps(msg))


async def run(ws: WebSocket, doc_id: UUID, user_id: UUID) -> None:
    """One person's live connection to a script, until they close it."""
    from services.compliance.evaluation import script_doc_service as svc

    doc, reviewers, access = await svc.get_access(doc_id, user_id)
    name = await svc._name(user_id) or "Someone"
    c = Conn(ws, user_id, name, doc_id)
    c.can_edit, c.can_note = access.can_edit, access.can_note

    # The very first person to open a new script gives the editor its starting text (from the template); claimed in the
    # database so two people opening at once on different workers can't both do it.
    claimed = await fetch_one("UPDATE script_documents SET seeded = TRUE WHERE id = $1 AND seeded = FALSE RETURNING id", doc_id)
    c.seeding = bool(claimed)

    rows = await fetch_all("SELECT channel, payload FROM script_document_updates WHERE document_id = $1 ORDER BY id", doc_id)
    updates: Dict[str, list] = {"text": [], "notes": []}
    for r in rows:
        updates.setdefault(r["channel"], []).append(base64.b64encode(r["payload"]).decode())
    # A claim by someone who left before saving anything is given up (below), so an empty text is seeded by the claimant only.
    _rooms.setdefault(doc_id, {})[c.id] = c
    await ws.send_text(json.dumps({
        "type": "init", "updates": updates, "seed": c.seeding, "you": {"can_edit": c.can_edit, "can_note": c.can_note},
        "user": {"id": str(user_id), "name": name},
    }))
    sender = asyncio.create_task(_send_loop(c))
    seeded_something = False
    try:
        while True:
            raw = await ws.receive_text()
            if len(raw) > MAX_MESSAGE_BYTES:
                continue
            try:
                m = json.loads(raw)
                kind = m.get("type")
            except Exception:
                continue
            if kind == "update":
                ch = m.get("ch")
                if ch not in ("text", "notes"):
                    continue
                allowed = c.can_note if ch == "notes" else (c.can_edit or c.seeding)
                if not allowed:
                    continue
                try:
                    payload = base64.b64decode(m.get("d") or "", validate=True)
                except Exception:
                    continue
                if len(payload) <= 2:  # an empty update carries nothing
                    continue
                row = await fetch_one(
                    "INSERT INTO script_document_updates (document_id, channel, payload, user_id) VALUES ($1, $2, $3, $4) RETURNING id",
                    doc_id, ch, payload, user_id)
                out = {"type": "update", "ch": ch, "d": m["d"], "from": str(user_id)}
                _deliver_local(doc_id, out, exclude=c.id)
                await _notify({"w": WORKER, "d": str(doc_id), "m": {"type": "update_ref", "ch": ch, "row": row["id"], "from": str(user_id)}})
                if ch == "text" and c.seeding and not seeded_something:
                    seeded_something = True  # the starting text is not a reviewer's change
                    continue
                await svc.note_change(doc_id, user_id, ch)
            elif kind == "claim_seed" and not c.seeding:
                # A browser that opened an empty script and wasn't given the starting text asks again (the person who was
                # may have left before saving it).
                # Also when an earlier claim never produced the text (its browser left or failed): while the page is still
                # empty, one connection at a time is asked to give it.
                busy = any(o.seeding for o in _rooms.get(doc_id, {}).values() if o is not c)
                claimed = None if busy else await fetch_one(
                    "UPDATE script_documents SET seeded = TRUE WHERE id = $1 AND NOT EXISTS "
                    "(SELECT 1 FROM script_document_updates WHERE document_id = $1 AND channel = 'text') RETURNING id", doc_id)
                if claimed:
                    c.seeding = True
                    await ws.send_text(json.dumps({"type": "seed"}))
            elif kind == "awareness":
                d = m.get("d")
                if isinstance(d, str) and len(d) < 6000:
                    await publish(doc_id, {"type": "awareness", "d": d, "from": str(user_id)}, exclude=c.id)
    except WebSocketDisconnect:
        pass
    except Exception:
        logger.debug("Script document connection ended", exc_info=True)
    finally:
        sender.cancel()
        _rooms.get(doc_id, {}).pop(c.id, None)
        if not _rooms.get(doc_id):
            _rooms.pop(doc_id, None)
        if c.seeding and not seeded_something:
            await execute(
                "UPDATE script_documents SET seeded = FALSE WHERE id = $1 AND NOT EXISTS "
                "(SELECT 1 FROM script_document_updates WHERE document_id = $1 AND channel = 'text') RETURNING id", doc_id)
        try:
            await publish(doc_id, {"type": "left", "user_id": str(user_id)})
        except Exception:
            pass
