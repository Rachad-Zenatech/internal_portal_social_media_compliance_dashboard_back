"""Microsoft Teams direct messages: the portal's notifications, delivered to people's Teams chat by a bot.

How it works (no extra libraries: the Bot Framework's plain web API):

- Each person has a 1:1 chat with the portal's bot. The chat is created when the person installs the bot (or an admin
  installs it for them) or writes to it. Teams then sends the bot an "activity" with the chat's address, which is stored
  (teams_conversations) and linked to the portal user by their Microsoft Entra id, or failing that their email.
- When the portal creates a notification for that user (see notification_service.create_notification), the same
  message is posted to their chat as a card with a button that opens the right page.
- The person can write "pause" to the bot to stop the messages and "resume" to get them again.

Safety: the endpoint Teams calls checks the Bot Framework's signed token, the tenant, and that the address it is told to
reply to belongs to Microsoft. The bot's credentials are only ever sent to such an address.

Off unless TEAMS_BOT_APP_ID, TEAMS_BOT_APP_PASSWORD and TEAMS_BOT_TENANT_ID are all set (see docs/teams-bot-setup.md).
"""

import asyncio
import logging
import os
import re
import time
from typing import Any, Dict, List, Optional
from urllib.parse import quote, urlsplit
from uuid import UUID

import httpx
import jwt

from models.notifications.notification_model import NotificationResponse
from postgresql_db.database import execute, fetch_one

logger = logging.getLogger(__name__)

TOKEN_URL = "https://login.microsoftonline.com/{tenant}/oauth2/v2.0/token"
BOT_SCOPE = "https://api.botframework.com/.default"
# The Bot Framework signs what it sends us with these keys, as "https://api.botframework.com".
BOT_KEYS_URL = "https://login.botframework.com/v1/.well-known/keys"
BOT_ISSUER = "https://api.botframework.com"
# Addresses the bot may reply to. Anything else is refused, so the bot's credentials can't be sent to a stranger.
SERVICE_HOSTS = tuple(h.strip().lower() for h in os.getenv(
    "TEAMS_SERVICE_URL_HOSTS",
    "smba.trafficmanager.net,botframework.com,botframework.us,botframework.azure.us,teams.microsoft.com,"
    "infra.gcc.teams.microsoft.com",
).split(",") if h.strip())
TIMEOUT_SEC = float(os.getenv("TEAMS_TIMEOUT_SEC", "10"))
MAX_TITLE = 120
MAX_MESSAGE = 600

_token: Dict[str, Any] = {"value": None, "expires": 0.0}
_jwks: Optional[jwt.PyJWKClient] = None


# --- Settings --------------------------------------------------------------------------------------------------------

def _app_id() -> str:
    return (os.getenv("TEAMS_BOT_APP_ID") or "").strip()


def _password() -> str:
    return (os.getenv("TEAMS_BOT_APP_PASSWORD") or "").strip()


def _tenant() -> str:
    return (os.getenv("TEAMS_BOT_TENANT_ID") or "").strip()


def enabled() -> bool:
    """Whether the bot is set up. When it isn't, nothing here does anything."""
    if os.getenv("TEAMS_ENABLED", "true").strip().lower() in {"0", "false", "no", "off"}:
        return False
    return bool(_app_id() and _password() and _tenant())


def portal_url() -> str:
    return (os.getenv("FRONTEND_URL") or "http://localhost:5179").rstrip("/")


def safe_service_url(url: Optional[str]) -> Optional[str]:
    """The address Teams told us to reply to, when it is a Microsoft one over https (else None)."""
    try:
        parts = urlsplit((url or "").strip())
    except ValueError:
        return None
    host = (parts.hostname or "").lower()
    if parts.scheme != "https" or not host or parts.username or parts.password:
        return None
    if not any(host == h or host.endswith("." + h) for h in SERVICE_HOSTS):
        return None
    return f"https://{parts.netloc}{parts.path}".rstrip("/")


# --- Checking what Teams sends us ------------------------------------------------------------------------------------

def _signing_key(token: str) -> Any:
    global _jwks
    if _jwks is None:
        _jwks = jwt.PyJWKClient(BOT_KEYS_URL, cache_keys=True, lifespan=3600)
    return _jwks.get_signing_key_from_jwt(token).key


async def verify_inbound(authorization: Optional[str], service_url: Optional[str]) -> bool:
    """Whether a request really comes from the Bot Framework, for this bot, about the address it names."""
    if not authorization or not authorization.lower().startswith("bearer "):
        return False
    token = authorization.split(" ", 1)[1].strip()
    try:
        key = await asyncio.to_thread(_signing_key, token)
        claims = jwt.decode(
            token, key, algorithms=["RS256"], audience=_app_id(), issuer=BOT_ISSUER,
            options={"require": ["exp", "iss", "aud"]},
        )
    except Exception as e:
        logger.info("Teams request refused: token not valid (%s)", str(e)[:120])
        return False
    claimed = claims.get("serviceurl")
    if claimed and (safe_service_url(claimed) or "") != (safe_service_url(service_url) or "-"):
        logger.info("Teams request refused: its address doesn't match its token")
        return False
    return True


# --- Talking to Teams ------------------------------------------------------------------------------------------------

async def _bot_token(force: bool = False) -> str:
    if not force and _token["value"] and time.time() < _token["expires"] - 60:
        return _token["value"]
    async with httpx.AsyncClient(timeout=TIMEOUT_SEC, trust_env=False) as client:
        response = await client.post(TOKEN_URL.format(tenant=_tenant()), data={
            "grant_type": "client_credentials", "client_id": _app_id(), "client_secret": _password(), "scope": BOT_SCOPE,
        })
    response.raise_for_status()
    body = response.json()
    _token["value"] = body["access_token"]
    _token["expires"] = time.time() + int(body.get("expires_in", 3000))
    return _token["value"]


def _conversation_path(service_url: str, conversation_id: str) -> str:
    return f"{service_url}/v3/conversations/{quote(conversation_id, safe=':@')}"


async def _call(method: str, url: str, *, json: Optional[Dict[str, Any]] = None) -> httpx.Response:
    """A call to the Bot Connector with the bot's token: a fresh token on 401, one more try after a 429."""
    async with httpx.AsyncClient(timeout=TIMEOUT_SEC, trust_env=False) as client:
        response = httpx.Response(500)
        for attempt in range(3):
            headers = {"Authorization": f"Bearer {await _bot_token(force=attempt > 0 and response.status_code == 401)}"}
            response = await client.request(method, url, json=json, headers=headers)
            if response.status_code == 429 and attempt < 2:
                await asyncio.sleep(min(float(response.headers.get("retry-after", "1") or 1), 5.0))
                continue
            if response.status_code == 401 and attempt == 0:
                continue
            return response
        return response


def card(title: str, message: str, link_url: Optional[str]) -> Dict[str, Any]:
    """An Adaptive Card: the title, the message and a button that opens the page in the portal."""
    body: List[Dict[str, Any]] = [
        {"type": "TextBlock", "text": title[:MAX_TITLE], "weight": "Bolder", "size": "Medium", "wrap": True},
        {"type": "TextBlock", "text": message[:MAX_MESSAGE], "wrap": True, "spacing": "Small"},
    ]
    out: Dict[str, Any] = {
        "type": "AdaptiveCard", "version": "1.4", "$schema": "http://adaptivecards.io/schemas/adaptive-card.json",
        "body": body,
    }
    if link_url:
        url = link_url if re.match(r"^https?://", link_url, re.IGNORECASE) else f"{portal_url()}/{link_url.lstrip('/')}"
        out["actions"] = [{"type": "Action.OpenUrl", "title": "Open in the portal", "url": url}]
    return out


async def _post_card(service_url: str, conversation_id: str, title: str, message: str,
                     link_url: Optional[str]) -> httpx.Response:
    return await _call("POST", f"{_conversation_path(service_url, conversation_id)}/activities", json={
        "type": "message",
        "text": f"{title}: {message}"[:MAX_MESSAGE],  # shown where cards aren't (notifications, chat list)
        "attachments": [{"contentType": "application/vnd.microsoft.card.adaptive",
                         "content": card(title, message, link_url)}],
    })


async def _say(service_url: str, conversation_id: str, text: str) -> None:
    try:
        await _call("POST", f"{_conversation_path(service_url, conversation_id)}/activities",
                    json={"type": "message", "text": text, "textFormat": "plain"})
    except Exception:
        logger.warning("Teams: couldn't reply in a chat", exc_info=True)


# --- Who is who ------------------------------------------------------------------------------------------------------

async def _member_email(service_url: str, conversation_id: str, member_id: str) -> Optional[str]:
    """The person's email, from the chat's roster (Teams sends only ids in the activity)."""
    try:
        response = await _call("GET", f"{_conversation_path(service_url, conversation_id)}/members/{quote(member_id, safe=':@')}")
        if response.status_code != 200:
            return None
        body = response.json()
        return (body.get("email") or body.get("userPrincipalName") or "").strip().lower() or None
    except Exception:
        return None


async def _save_conversation(aad_object_id: str, conversation_id: str, service_url: str, tenant_id: Optional[str],
                             email: Optional[str]) -> bool:
    """Remember the chat, linked to the portal user it belongs to. Returns whether a portal user was found."""
    user = await fetch_one(
        """
        SELECT id FROM users
        WHERE entra_oid = $1 OR ($2::text IS NOT NULL AND lower(email) = $2)
        ORDER BY (entra_oid = $1) DESC NULLS LAST LIMIT 1
        """,
        aad_object_id, email,
    )
    await execute(
        """
        INSERT INTO teams_conversations (user_id, aad_object_id, conversation_id, service_url, tenant_id, email)
        VALUES ($1, $2, $3, $4, $5, $6)
        ON CONFLICT (aad_object_id) DO UPDATE SET
            user_id = COALESCE(EXCLUDED.user_id, teams_conversations.user_id),
            conversation_id = EXCLUDED.conversation_id, service_url = EXCLUDED.service_url,
            tenant_id = EXCLUDED.tenant_id, email = COALESCE(EXCLUDED.email, teams_conversations.email),
            active = TRUE, last_error = NULL, updated_at = NOW()
        """,
        user["id"] if user else None, aad_object_id, conversation_id, service_url, tenant_id, email,
    )
    return user is not None


async def _set_enabled(aad_object_id: str, on: bool) -> None:
    await execute("UPDATE teams_conversations SET enabled = $2, updated_at = NOW() WHERE aad_object_id = $1",
                  aad_object_id, on)


async def _deactivate(conversation_row_id: int, reason: str) -> None:
    await execute("UPDATE teams_conversations SET active = FALSE, last_error = $2, updated_at = NOW() WHERE id = $1",
                  conversation_row_id, reason[:300])


# --- What people write to the bot ------------------------------------------------------------------------------------

WELCOME = ("Hi! I'll send you your compliance portal notifications here (posts sent for review, changes requested, "
           "decisions). Write \"pause\" to stop them and \"resume\" to start again.")
UNKNOWN_USER = ("I can't find a portal account for you yet, so I can't send you notifications. Sign in to the portal "
                "once with your work account and write to me again.")


def _command(activity: Dict[str, Any]) -> str:
    text = re.sub(r"<at>.*?</at>", " ", activity.get("text") or "", flags=re.IGNORECASE)
    return " ".join(text.lower().split()).strip(" .!")


async def handle_activity(activity: Dict[str, Any]) -> None:
    """React to what Teams sends the bot: a person installing it, or writing to it."""
    conversation = activity.get("conversation") or {}
    sender = activity.get("from") or {}
    recipient = activity.get("recipient") or {}
    service_url = safe_service_url(activity.get("serviceUrl"))
    tenant = (((activity.get("channelData") or {}).get("tenant")) or {}).get("id") or (conversation.get("tenantId"))
    if not service_url or (tenant or "").lower() != _tenant().lower():
        return  # someone else's tenant, or an address that isn't Microsoft's
    kind = activity.get("type")
    conversation_id = conversation.get("id") or ""

    # Only 1:1 chats: the notifications are personal.
    if conversation.get("conversationType") != "personal":
        if kind == "message":
            await _say(service_url, conversation_id, "I only send direct messages. Write to me in a chat of your own.")
        return

    person = sender
    if kind == "conversationUpdate":
        added = [m for m in (activity.get("membersAdded") or []) if m.get("id") != recipient.get("id")]
        if not added:
            return
        person = added[0]
    elif kind == "installationUpdate":
        if activity.get("action") != "add":
            return
    elif kind != "message":
        return

    oid = person.get("aadObjectId") or sender.get("aadObjectId")
    if not oid:
        return
    email = await _member_email(service_url, conversation_id, person.get("id") or sender.get("id") or "")
    linked = await _save_conversation(oid, conversation_id, service_url, tenant, email)

    if kind != "message":
        await _say(service_url, conversation_id, WELCOME if linked else UNKNOWN_USER)
        return
    command = _command(activity)
    if command in ("pause", "stop", "mute"):
        await _set_enabled(oid, False)
        await _say(service_url, conversation_id, "Okay, I've paused your notifications. Write \"resume\" to start again.")
    elif command in ("resume", "start", "unmute"):
        await _set_enabled(oid, True)
        await _say(service_url, conversation_id, "Notifications are back on." if linked else UNKNOWN_USER)
    else:
        await _say(service_url, conversation_id, WELCOME if linked else UNKNOWN_USER)


# --- Sending the portal's notifications ------------------------------------------------------------------------------

async def notify_user(notification: NotificationResponse) -> bool:
    """Post a portal notification to its user's Teams chat, if they have one and haven't paused it. Never raises: a
    message that can't be delivered must not break what the person was doing."""
    if not enabled():
        return False
    try:
        row = await fetch_one(
            "SELECT id, conversation_id, service_url FROM teams_conversations "
            "WHERE user_id = $1 AND active AND enabled",
            UUID(str(notification.user_id)),
        )
        if not row:
            return False
        service_url = safe_service_url(row["service_url"])
        if not service_url:
            await _deactivate(row["id"], "The stored address isn't a Microsoft one.")
            return False
        response = await _post_card(service_url, row["conversation_id"], notification.title, notification.message,
                                    notification.link_url)
        if response.status_code in (200, 201, 202):
            await execute("UPDATE teams_conversations SET last_sent_at = NOW(), last_error = NULL WHERE id = $1", row["id"])
            return True
        if response.status_code in (403, 404):
            # The bot was removed, or the chat is gone: stop trying until the person writes to the bot again.
            await _deactivate(row["id"], f"Teams answered {response.status_code}: {response.text[:200]}")
        else:
            logger.warning("Teams: message not delivered (%s): %s", response.status_code, response.text[:200])
        return False
    except Exception:
        logger.warning("Teams: couldn't send a notification", exc_info=True)
        return False


async def status_for(user_id: str) -> Dict[str, Any]:
    """What the portal shows a person about their Teams messages."""
    row = await fetch_one(
        "SELECT enabled, active, last_sent_at, last_error FROM teams_conversations WHERE user_id = $1", UUID(str(user_id)),
    ) if enabled() else None
    return {
        "available": enabled(),
        "linked": bool(row and row["active"]),
        "enabled": bool(row["enabled"]) if row else True,
        "last_sent_at": row["last_sent_at"].isoformat() if row and row["last_sent_at"] else None,
        "problem": row["last_error"] if row and not row["active"] else None,
    }


async def set_enabled_for(user_id: str, on: bool) -> None:
    await execute("UPDATE teams_conversations SET enabled = $2, updated_at = NOW() WHERE user_id = $1",
                  UUID(str(user_id)), on)
