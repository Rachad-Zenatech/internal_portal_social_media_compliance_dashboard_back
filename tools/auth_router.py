import hashlib
import logging
import os
import re
import secrets
from fastapi import APIRouter, Request, HTTPException, Depends, Response
from fastapi.responses import JSONResponse, RedirectResponse

from authlib.integrations.starlette_client import OAuth
from starlette.config import Config
from services.shared.auth_service import (
    create_access_token,
    getCurrentUserFromMicrosoftClaims,
    upsertMicrosoftUser,
    getUserRoles,
    getUserNavigationPermissions,
    getUserToolPermissions,
    clear_access_token_cookie,
    get_current_user_id_dependency,
    set_access_token_cookie,
    AUTH_COOKIE_NAME,
)
from services.microsoft_graph.graph_service import fetch_me_profile
from services.shared.rbac_service import log_login_activity
from postgresql_db.database import get_pool
from uuid import UUID
from pydantic import BaseModel, Field

router = APIRouter()
logger = logging.getLogger(__name__)

# One-time sign-in codes (see postgresql_db/setup_login_codes_schema.py).
LOGIN_CODE_TTL_SECONDS = 120
_NONCE_RE = re.compile(r"^[A-Za-z0-9_-]{32,128}$")


def _sha256(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()

# Try to load from environment
MICROSOFT_CLIENT_ID = os.getenv("MICROSOFT_CLIENT_ID", "")
MICROSOFT_CLIENT_SECRET = os.getenv("MICROSOFT_CLIENT_SECRET", "")
MICROSOFT_TENANT_ID = os.getenv("MICROSOFT_TENANT_ID", "common")

config_dict = {
    "MICROSOFT_CLIENT_ID": MICROSOFT_CLIENT_ID,
    "MICROSOFT_CLIENT_SECRET": MICROSOFT_CLIENT_SECRET
}
config = Config(environ=config_dict)
oauth = OAuth(config)

CONF_URL = f"https://login.microsoftonline.com/{MICROSOFT_TENANT_ID}/v2.0/.well-known/openid-configuration"

try:
    oauth.register(
        name='microsoft',
        server_metadata_url=CONF_URL,
        client_kwargs={
            'scope': 'openid email profile User.Read'
        }
    )
except Exception:
    logger.exception(
        "Could not register Microsoft OAuth client",
        extra={"event": "microsoft_oauth_registration_failed"},
    )

@router.get("/auth/microsoft/login")
async def microsoft_login(request: Request, nonce: str = ""):
    # Random value the sign-in page keeps in the browser; the code issued after sign-in only works with it, so a
    # sign-in link made in someone else's browser can't sign you into their account.
    if not _NONCE_RE.match(nonce):
        raise HTTPException(status_code=400, detail="Start signing in from the sign-in page")
    request.session["login_nonce_hash"] = _sha256(nonce)
    redirect_uri = os.getenv("MICROSOFT_REDIRECT_URI")
    if not redirect_uri:
        logger.error(
            "MICROSOFT_REDIRECT_URI is not configured",
            extra={"event": "microsoft_oauth_misconfigured"},
        )
        raise HTTPException(status_code=500, detail="Microsoft sign-in is not configured")
    # oauth.microsoft is only present when registration succeeded (valid client id/secret).
    client = getattr(oauth, "microsoft", None)
    if client is None:
        logger.error(
            "Microsoft OAuth client is not registered",
            extra={"event": "microsoft_oauth_not_registered"},
        )
        raise HTTPException(status_code=500, detail="Microsoft sign-in is not configured")
    try:
        return await client.authorize_redirect(request, redirect_uri, prompt="select_account")
    except Exception as exc:
        logger.exception(
            "Failed to start Microsoft OAuth redirect",
            extra={"event": "microsoft_oauth_login_failed"},
        )
        raise HTTPException(status_code=502, detail="Could not reach Microsoft sign-in") from exc

@router.get("/auth/microsoft/callback")
async def microsoft_callback(request: Request):
    try:
        token = await oauth.microsoft.authorize_access_token(request)
        user_info = token.get('userinfo') or {}
    except Exception as exc:
        logger.warning(
            "Microsoft OAuth callback failed",
            extra={
                "event": "microsoft_oauth_callback_failed",
                "error_type": type(exc).__name__,
            },
        )
        raise HTTPException(status_code=400, detail="Microsoft sign-in failed. Please try again.") from exc

    # Reuse the access token to fetch the employee's Graph profile
    graph_profile = None
    graph_access_token = token.get('access_token')
    if graph_access_token:
        try:
            graph_profile = await fetch_me_profile(graph_access_token)
        except Exception:
            logger.exception(
                "Microsoft Graph profile fetch failed during login; continuing with ID token claims only",
                extra={"event": "graph_me_fetch_exception"},
            )
            graph_profile = None

    claims = getCurrentUserFromMicrosoftClaims(user_info)
    if graph_profile:
        if not claims.get('email'):
            claims['email'] = (graph_profile.get('email') or graph_profile.get('user_principal_name') or '').lower().strip()
        if not claims.get('name'):
            claims['name'] = (graph_profile.get('display_name') or '').strip()
        if not claims.get('oid'):
            claims['oid'] = graph_profile.get('object_id')

    email = (claims.get('email') or '').lower().strip()
    claims['email'] = email

    if not email:
        raise HTTPException(status_code=403, detail="Email not provided by Microsoft")

    ip_address = request.client.host if request.client else None
    user_agent = request.headers.get("user-agent")

    # Check allowed email domains if configured
    allowed_domains_str = os.getenv("ALLOWED_EMAIL_DOMAINS", "").strip()
    if allowed_domains_str and allowed_domains_str != "*":
        allowed_domains = [d.strip().lower().lstrip("@") for d in allowed_domains_str.split(",") if d.strip()]
        user_domain = email.split("@")[-1].lower() if "@" in email else ""
        if user_domain not in allowed_domains:
            await log_login_activity(email, None, False, f"Domain @{user_domain} is not authorized", ip_address, user_agent)
            raise HTTPException(status_code=403, detail=f"Email domain @{user_domain} is not authorized for this application")

    # Connect Microsoft Entra identity to local RBAC
    user = await upsertMicrosoftUser(claims, graph_profile)

    frontend_url = (os.getenv("FRONTEND_URL") or "http://localhost:5179").rstrip("/")

    if not user.get("is_active"):
        await log_login_activity(email, user["id"], False, "Account pending/inactive", ip_address, user_agent)
        return RedirectResponse(f"{frontend_url}/login?status=ACCESS_PENDING&error=account_pending")

    await log_login_activity(email, user["id"], True, None, ip_address, user_agent)

    nonce_hash = request.session.pop("login_nonce_hash", None)
    if not nonce_hash:
        return RedirectResponse(f"{frontend_url}/login?error=login_expired")
    # The redirect carries a one-time code, never the session token: URLs end up in history and logs.
    code = secrets.token_urlsafe(32)
    pool = get_pool()
    async with pool.acquire() as conn:
        await conn.execute("DELETE FROM login_codes WHERE expires_at < now() - interval '1 day'")
        await conn.execute(
            """
            INSERT INTO login_codes (code_hash, nonce_hash, user_id, expires_at)
            VALUES ($1, $2, $3, now() + make_interval(secs => $4))
            """,
            _sha256(code), nonce_hash, user["id"], LOGIN_CODE_TTL_SECONDS,
        )
    access_token = create_access_token(user["id"], user["is_super_admin"])
    response = RedirectResponse(f"{frontend_url}/login?status=success&code={code}")
    set_access_token_cookie(response, access_token)
    return response


class LoginCodeExchange(BaseModel):
    code: str = Field(min_length=16, max_length=128)
    nonce: str = Field(min_length=32, max_length=128)


@router.post("/auth/exchange")
async def exchange_login_code(data: LoginCodeExchange):
    """Swap the one-time code from the sign-in redirect, with the nonce this browser made when it started signing
    in, for a session token. Each code works once, for two minutes."""
    pool = get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            UPDATE login_codes c SET used_at = now()
            FROM users u
            WHERE c.code_hash = $1 AND c.nonce_hash = $2 AND c.used_at IS NULL AND c.expires_at > now()
              AND u.id = c.user_id AND u.is_active AND u.deleted_at IS NULL
            RETURNING u.id, u.is_super_admin
            """,
            _sha256(data.code), _sha256(data.nonce),
        )
    if not row:
        # 400, not 401: this isn't an expired session, just a sign-in link that can't be used.
        raise HTTPException(status_code=400, detail="This sign-in link has expired or was already used. Sign in again.")
    return {"token": create_access_token(row["id"], bool(row["is_super_admin"]))}

@router.get("/auth/bootstrap")
async def get_auth_bootstrap(user_id: UUID = Depends(get_current_user_id_dependency)):
    pool = get_pool()
    async with pool.acquire() as conn:
        user = await conn.fetchrow("SELECT id, email, full_name, is_active, is_super_admin, auth_provider FROM users WHERE id = $1", user_id)
        if not user:
            raise HTTPException(status_code=404, detail="User not found")

        user_dict = {
            "id": str(user["id"]),
            "email": user["email"],
            "fullName": user["full_name"],
            "isActive": user["is_active"],
            "isSuperAdmin": user["is_super_admin"],
            "authProvider": user["auth_provider"]
        }

        roles = await getUserRoles(user_id)
        role_ids = [r["id"] for r in roles]

        navigation = await getUserNavigationPermissions(role_ids) if not user["is_super_admin"] else await get_all_navigation_items()
        tools = await getUserToolPermissions(role_ids) if not user["is_super_admin"] else await get_all_mcp_tools()

        return {
            "user": user_dict,
            "roles": roles,
            "navigation": navigation,
            "tools": tools
        }

async def get_all_navigation_items():
    pool = get_pool()
    async with pool.acquire() as conn:
        items = await conn.fetch("SELECT id, code, name, route_path as \"routePath\", parent_code as \"parentCode\", display_order as \"displayOrder\", icon FROM navigation_items WHERE is_active = true ORDER BY display_order")
        return [dict(i) for i in items]

async def get_all_mcp_tools():
    pool = get_pool()
    async with pool.acquire() as conn:
        tools = await conn.fetch("SELECT id, code, name, 'execute' as \"accessLevel\", '{}'::jsonb as conditions FROM mcp_tools WHERE is_active = true")
        return [dict(t) for t in tools]

@router.post("/auth/heartbeat")
async def heartbeat(
    response: Response,
    user_id: UUID = Depends(get_current_user_id_dependency),
):
    pool = get_pool()
    async with pool.acquire() as conn:
        user = await conn.fetchrow(
            """
            UPDATE users
            SET last_activity_at = now()
            WHERE id = $1
              AND deleted_at IS NULL
              AND is_active = TRUE
            RETURNING is_super_admin
            """,
            user_id,
        )
    if not user:
        raise HTTPException(status_code=401, detail="User is inactive or no longer exists")
    refreshed_token = create_access_token(user_id, bool(user["is_super_admin"]))
    set_access_token_cookie(response, refreshed_token)
    return {"status": "ok"}

@router.post("/auth/link-token")
async def link_token(user_id: UUID = Depends(get_current_user_id_dependency)):
    """A short-lived token for loading media and the notification stream by URL (<img>, <video>, EventSource
    can't send headers). It only works for those GET URLs."""
    from services.shared.auth_service import create_link_token
    token, expires = create_link_token(user_id)
    return {"token": token, "expires_at": expires.isoformat()}

@router.post("/auth/logout")
async def logout(request: Request):
    auth_header = request.headers.get("Authorization")
    token = None
    if auth_header and auth_header.startswith("Bearer "):
        token = auth_header.split(" ")[1]
    else:
        token = request.cookies.get(AUTH_COOKIE_NAME)

    if not token:
        try:
            form = await request.form()
            token = form.get("token")
        except:
            pass

    response = JSONResponse({"status": "success"})
    clear_access_token_cookie(response)
    request.session.clear()

    if token:
        import jwt
        from services.shared.auth_service import JWT_SECRET, JWT_ALGORITHM
        from services.shared.cache_service import emit_cache_invalidation
        try:
            payload = jwt.decode(
                token,
                JWT_SECRET,
                algorithms=[JWT_ALGORITHM],
                options={"verify_exp": False, "require": ["sub", "iat"], "verify_iss": False}
            )
        except jwt.PyJWTError:
            payload = None

        if payload:
            try:
                user_id = UUID(payload["sub"])
                await emit_cache_invalidation(scope="permissions", key=f"perms:{user_id}")
                pool = get_pool()
                async with pool.acquire() as conn:
                    # Get user's last_login_at
                    user = await conn.fetchrow("SELECT id FROM users WHERE id = $1", user_id)
                    if user:
                        # Every token issued before now stops working (see get_current_user_id_dependency).
                        await conn.execute("UPDATE users SET last_logout_at = now() WHERE id = $1", user_id)
                        from services.shared.auth_service import _user_auth_cache
                        _user_auth_cache.pop(user_id, None)
                        # Update the corresponding login activity log to set logout_at
                        await conn.execute("""
                            UPDATE login_activity_logs
                            SET logout_at = now()
                            WHERE id = (
                                SELECT id FROM login_activity_logs
                                WHERE user_id = $1 AND success = true AND logout_at IS NULL
                                ORDER BY created_at DESC
                                LIMIT 1
                            )
                        """, user_id)
            except Exception:
                pass

    return response
