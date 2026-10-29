"""Microsoft Graph API client for employee profile sync.

Reuses the existing Microsoft Entra App Registration (MICROSOFT_CLIENT_ID /
MICROSOFT_CLIENT_SECRET / MICROSOFT_TENANT_ID, see tools/auth_router.py) —
no separate app registration or login flow is introduced here.

Two acquisition paths are used:
  - Delegated: the access token already issued during the existing OAuth
    callback (scope already includes User.Read) is reused as-is to call
    GET /v1.0/me for the signed-in employee. No extra token round trip.
  - Application (client credentials): used only by the background sync
    (services/microsoft_graph/graph_sync_job.py) to read other users' profiles via
    GET /v1.0/users, which requires the User.Read.All application
    permission with admin consent. If that consent hasn't been granted,
    get_app_only_token() logs and returns None rather than raising, so the
    background job degrades to a no-op instead of crashing the app.
"""

import asyncio
import logging
import os
from typing import Optional

import httpx

logger = logging.getLogger(__name__)

GRAPH_BASE_URL = "https://graph.microsoft.com/v1.0"
_SELECT_FIELDS = "id,displayName,mail,userPrincipalName,department,jobTitle,accountEnabled"

_MAX_RETRIES = 1
_REQUEST_TIMEOUT = 2.0


def _normalize_profile(raw: dict) -> dict:
    return {
        "object_id": raw.get("id"),
        "display_name": raw.get("displayName"),
        "email": (raw.get("mail") or raw.get("userPrincipalName") or "").lower() or None,
        "user_principal_name": raw.get("userPrincipalName"),
        "department": raw.get("department"),
        "job_title": raw.get("jobTitle"),
        "account_enabled": raw.get("accountEnabled"),
    }


async def fetch_me_profile(access_token: str) -> Optional[dict]:
    """Fetch the signed-in employee's own profile via GET /v1.0/me.

    Best-effort: returns None on any failure so callers (the OAuth callback)
    can fall back to ID-token claims and never block login on Graph.
    """
    url = f"{GRAPH_BASE_URL}/me"
    params = {"$select": _SELECT_FIELDS}
    headers = {"Authorization": f"Bearer {access_token}"}

    for attempt in range(1, _MAX_RETRIES + 1):
        try:
            async with httpx.AsyncClient(timeout=_REQUEST_TIMEOUT) as client:
                response = await client.get(url, params=params, headers=headers)
            if response.status_code == 429:
                retry_after = float(response.headers.get("Retry-After", "1"))
                logger.warning(
                    "Graph /me throttled",
                    extra={"event": "graph_me_throttled", "retry_after": retry_after},
                )
                await asyncio.sleep(retry_after)
                continue
            response.raise_for_status()
            return _normalize_profile(response.json())
        except httpx.HTTPStatusError as exc:
            logger.warning(
                "Graph /me request rejected",
                extra={"event": "graph_me_failed", "status_code": exc.response.status_code},
            )
            return None
        except (httpx.TimeoutException, httpx.TransportError) as exc:
            if attempt < _MAX_RETRIES:
                logger.warning(
                    "Graph /me request errored, retrying",
                    extra={"event": "graph_me_retry", "attempt": attempt, "error_type": type(exc).__name__},
                )
                await asyncio.sleep(0.5 * attempt)
            else:
                logger.warning(
                    "Graph /me request errored, giving up",
                    extra={"event": "graph_me_error", "error_type": type(exc).__name__},
                )
        except Exception:
            logger.exception(
                "Unexpected error calling Graph /me",
                extra={"event": "graph_me_unexpected_error"},
            )
            return None
    return None


async def get_app_only_token() -> Optional[str]:
    """Acquire an application-only Graph token via client credentials, using
    the same Entra App Registration as the delegated login flow.

    Requires the User.Read.All application permission to have admin consent.
    Returns None (logged) if that hasn't happened yet or credentials are
    missing, so the background sync can skip a run instead of crashing.
    """
    tenant_id = os.getenv("MICROSOFT_TENANT_ID", "common")
    client_id = os.getenv("MICROSOFT_CLIENT_ID", "")
    client_secret = os.getenv("MICROSOFT_CLIENT_SECRET", "")
    if not client_id or not client_secret or tenant_id in ("common", "organizations", "consumers"):
        logger.warning(
            "Graph app-only token not configured (missing client credentials or a tenant-specific MICROSOFT_TENANT_ID)",
            extra={"event": "graph_app_token_unconfigured"},
        )
        return None

    token_url = f"https://login.microsoftonline.com/{tenant_id}/oauth2/v2.0/token"
    data = {
        "client_id": client_id,
        "client_secret": client_secret,
        "scope": "https://graph.microsoft.com/.default",
        "grant_type": "client_credentials",
    }

    for attempt in range(1, _MAX_RETRIES + 1):
        try:
            async with httpx.AsyncClient(timeout=_REQUEST_TIMEOUT) as client:
                response = await client.post(token_url, data=data)
            if response.status_code == 429:
                retry_after = float(response.headers.get("Retry-After", "1"))
                await asyncio.sleep(retry_after)
                continue
            response.raise_for_status()
            return response.json().get("access_token")
        except httpx.HTTPStatusError as exc:
            logger.error(
                "Graph app-only token request rejected — check admin consent for User.Read.All",
                extra={"event": "graph_app_token_rejected", "status_code": exc.response.status_code},
            )
            return None
        except (httpx.TimeoutException, httpx.TransportError) as exc:
            if attempt < _MAX_RETRIES:
                await asyncio.sleep(0.5 * attempt)
            else:
                logger.error(
                    "Graph app-only token request failed",
                    extra={"event": "graph_app_token_error", "error_type": type(exc).__name__},
                )
        except Exception:
            logger.exception(
                "Unexpected error acquiring Graph app-only token",
                extra={"event": "graph_app_token_unexpected_error"},
            )
            return None
    return None


async def search_users(query: str = "") -> list:
    """Search Entra users by display name or email using Graph $search.
    Falls back to searching local registered users if Graph is unavailable or lacks tenant admin consent.
    """
    q = (query or "").strip().replace("'", "")
    results_map = {}

    # 1. Search local organization users
    try:
        from postgresql_db.database import fetch_all
        if q:
            like_q = f"%{q}%"
            rows = await fetch_all(
                """
                SELECT id, microsoft_object_id, email, full_name, job_title, department
                FROM users
                WHERE is_active = true
                  AND (
                      email ILIKE $1
                      OR full_name ILIKE $1
                      OR department ILIKE $1
                      OR job_title ILIKE $1
                  )
                ORDER BY full_name, email
                LIMIT 50
                """,
                like_q,
            )
        else:
            rows = await fetch_all(
                """
                SELECT id, microsoft_object_id, email, full_name, job_title, department
                FROM users
                WHERE is_active = true
                ORDER BY full_name, email
                LIMIT 50
                """
            )
        for r in rows:
            email = (r["email"] or "").lower().strip()
            key = email or str(r["id"])
            results_map[key] = {
                "object_id": r["microsoft_object_id"] or str(r["id"]),
                "display_name": r["full_name"] or "",
                "email": email,
                "job_title": r["job_title"] or "",
                "department": r["department"] or "",
                "user_principal_name": email,
            }
    except Exception as exc:
        logger.debug(f"Local user search note: {exc}")

    # 2. Query Microsoft Graph if query length >= 2
    if q and len(q) >= 2:
        token = await get_app_only_token()
        if token:
            url = f"{GRAPH_BASE_URL}/users"
            headers = {
                "Authorization": f"Bearer {token}",
                "ConsistencyLevel": "eventual",
            }
            params = {
                "$search": f'"displayName:{q}" OR "mail:{q}" OR "userPrincipalName:{q}"',
                "$select": _SELECT_FIELDS,
                "$top": 25,
            }
            try:
                async with httpx.AsyncClient(timeout=_REQUEST_TIMEOUT) as client:
                    resp = await client.get(url, params=params, headers=headers)
                    if resp.status_code == 200:
                        for u in resp.json().get("value", []):
                            prof = _normalize_profile(u)
                            if prof["email"] and prof["email"] not in results_map:
                                results_map[prof["email"]] = prof
            except Exception as exc:
                logger.debug(f"Graph $search note: {exc}")

    return list(results_map.values())
async def fetch_users_page(access_token: str, next_link: Optional[str] = None) -> Optional[dict]:
    """Fetch one page of /v1.0/users (or follow a previous @odata.nextLink).

    Returns {"value": [normalized profile, ...], "next_link": str | None},
    or None if this page could not be fetched after retries.
    """
    url = next_link or f"{GRAPH_BASE_URL}/users"
    params = None if next_link else {"$select": _SELECT_FIELDS, "$top": 999}
    headers = {"Authorization": f"Bearer {access_token}"}

    for attempt in range(1, _MAX_RETRIES + 1):
        try:
            async with httpx.AsyncClient(timeout=_REQUEST_TIMEOUT) as client:
                response = await client.get(url, params=params, headers=headers)
            if response.status_code == 429:
                retry_after = float(response.headers.get("Retry-After", "2"))
                logger.warning(
                    "Graph /users throttled",
                    extra={"event": "graph_users_throttled", "retry_after": retry_after},
                )
                await asyncio.sleep(retry_after)
                continue
            response.raise_for_status()
            body = response.json()
            return {
                "value": [_normalize_profile(u) for u in body.get("value", [])],
                "next_link": body.get("@odata.nextLink"),
            }
        except httpx.HTTPStatusError as exc:
            logger.error(
                "Graph /users page request failed",
                extra={"event": "graph_users_failed", "status_code": exc.response.status_code},
            )
            return None
        except (httpx.TimeoutException, httpx.TransportError) as exc:
            if attempt < _MAX_RETRIES:
                await asyncio.sleep(0.5 * attempt)
            else:
                logger.error(
                    "Graph /users page request errored",
                    extra={"event": "graph_users_error", "error_type": type(exc).__name__},
                )
        except Exception:
            logger.exception(
                "Unexpected error calling Graph /users",
                extra={"event": "graph_users_unexpected_error"},
            )
            return None
    return None
