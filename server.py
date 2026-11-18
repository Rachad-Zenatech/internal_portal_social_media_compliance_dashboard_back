import asyncio
import hashlib
import logging
import os
import re
import sys
import time
from collections import defaultdict
from contextlib import asynccontextmanager
from typing import Optional
from uuid import uuid4

if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())

from fastapi import Depends, FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastmcp import FastMCP
from starlette.middleware.sessions import SessionMiddleware

from postgresql_db.database import close_pool, create_pool, fetch_one
from services.shared.core.logging_service import (
    asyncio_exception_handler,
    configure_logging,
    request_id_ctx,
)
from services.shared.core.startup_service import run_schema_setups, spawn_background
from services.shared.core.lifecycle_background_service import start_background_services
from services.shared.auth.auth_service import (
    get_current_user_id_dependency,
)

from services.shared.auth.secret_config import require_secret

# FastAPI routers
from tools.shared.core.search import router as search_router
from tools.notifications.message_hub_router import message_hub_router
from tools.shared.auth.rbac_router import router as rbac_router
from tools.compliance.submissions.approval_path_router import router as approval_path_router
from tools.compliance.submissions.context_router import router as context_router
from tools.compliance.analytics.listening_router import router as listening_router
from tools.compliance.analytics.performance_router import router as performance_router
from tools.shared.auth.auth_router import router as auth_router
from tools.notifications.notification_router import notification_router
from tools.notifications.teams_router import teams_bot_router, teams_router
from tools.shared.core.observability import router as observability_router
from tools.shared.core.log_router import router as log_router
from tools.microsoft_graph.graph_router import router as graph_router
from tools.shared.auth.oauth_router import router as oauth_router
from tools.compliance.submissions.compliance_router import router as compliance_router
from tools.compliance.submissions.submission_router import router as submission_router
from tools.compliance.submissions.source_routers import router as source_routers
from tools.compliance.submissions.script_review_router import router as script_review_router
from tools.compliance.submissions.script_batch_router import router as script_batch_router
from tools.compliance.submissions.script_doc_router import router as script_doc_router
from tools.compliance.analytics.canva_router import router as canva_router
from tools.ai.vector_store_router import router as vector_store_router
from tools.ai.ai_status_router import router as ai_status_router
from tools.ai.chat_router import router as chat_router
from tools.compliance.analytics.sentiment_router import router as sentiment_router

# MCP tools & AI functions
from mcp_tools import register_all as register_mcp_tools
from services.ai.observability import ai_observability

configure_logging()
logger = logging.getLogger(__name__)


def _graph_background_sync_enabled() -> bool:
    return os.getenv("GRAPH_BACKGROUND_SYNC_ENABLED", "false").strip().lower() in {"1", "true", "yes", "on"}


def _env_flag(name: str, default: str) -> bool:
    return os.getenv(name, default).strip().lower() in {"1", "true", "yes", "on"}


@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("Application startup beginning", extra={"event": "application_starting"})
    loop = asyncio.get_running_loop()
    previous_exception_handler = loop.get_exception_handler()
    loop.set_exception_handler(asyncio_exception_handler)
    try:
        pool = await create_pool()
        # Apply schema changes before serving requests in every environment.
        should_auto_migrate = _env_flag(
            "AUTO_MIGRATE_SCHEMA",
            "true",
        )
        if should_auto_migrate:
            logger.info("Running automated schema verification and migration", extra={"event": "schema_migration_start"})
            await run_schema_setups(pool)
        else:
            logger.info("Skipping startup schema auto-migration (AUTO_MIGRATE_SCHEMA=false)", extra={"event": "schema_migration_skipped"})
    except Exception:
        logger.exception("Application startup failed", extra={"event": "application_startup_failed"})
        raise

    background_tasks = []

    start_background_services(
        background_tasks,
        spawn_background,
        logger,
        _graph_background_sync_enabled(),
    )

    # 4. Initialize Event-Driven MQTT Service Presence & Bus
    from services.notifications import mqtt_presence as mqtt_module
    from services.notifications.mqtt_presence import MqttServicePresence
    presence_instance = MqttServicePresence(
        service_name=os.getenv("SERVICE_NAME", "social-media"),
        instance_id=os.getenv("SERVICE_INSTANCE_ID", "social-media-01"),
    )
    mqtt_module.social_media_presence = presence_instance
    await presence_instance.start()

    # 5. Initialize RabbitMQ Manager (Cross-Service Asynchronous Messaging)
    from services.notifications.rabbitmq_service import rabbitmq_manager
    await rabbitmq_manager.connect()

    logger.info("Application startup complete", extra={"event": "application_started"})
    try:
        # The MCP endpoint (/mcp) runs its session manager inside the app's lifespan.
        async with mcp_http_app.lifespan(app):
            yield
    finally:
        logger.info("Application shutdown beginning", extra={"event": "application_stopping"})
        try:
            await rabbitmq_manager.close()
        except Exception as rmq_err:
            logger.warning(f"Error closing RabbitMQ manager: {rmq_err}")
        try:
            await presence_instance.stop()
        except Exception as mqtt_err:
            logger.warning(f"Error stopping MQTT presence: {mqtt_err}")
        for task in background_tasks:
            task.cancel()
        await asyncio.gather(*background_tasks, return_exceptions=True)
        await ai_observability.flush()
        await close_pool()
        loop.set_exception_handler(previous_exception_handler)
        logger.info("Application shutdown complete", extra={"event": "application_stopped"})


_is_prod = os.getenv("APP_ENV", "development").lower() == "production"
HTTPS_MODE = _env_flag("HTTPS_MODE", "true" if _is_prod else "false")
_HSTS_ENABLED = HTTPS_MODE
_expose_docs = _env_flag("EXPOSE_API_DOCS", "false" if _is_prod else "true")

CONTENT_SECURITY_POLICY = (
    "default-src 'self'; script-src 'self' 'unsafe-inline' https://cdn.jsdelivr.net; "
    "style-src 'self' 'unsafe-inline' https://cdn.jsdelivr.net; img-src 'self' data: https:; "
    "worker-src 'self' blob:; frame-ancestors 'none'"
    if _expose_docs
    else "default-src 'none'; frame-ancestors 'none'; base-uri 'none'; form-action 'none'"
)

app = FastAPI(
    title="Social Media Compliance Backend",
    version="1.0.0",
    lifespan=lifespan,
    docs_url="/docs" if _expose_docs else None,
    redoc_url="/redoc" if _expose_docs else None,
    openapi_url="/openapi.json" if _expose_docs else None,
)

# MCP server setup
mcp = FastMCP("Social Media Compliance MCP Server")
register_mcp_tools(mcp)
# Streamable HTTP endpoint at /mcp/: clients send the portal's access token (Bearer); tools are filtered and checked
# per role, traced and redacted by mcp_tools.middleware.
mcp_http_app = mcp.http_app(path="/")
app.mount("/mcp", mcp_http_app)


# --- Basic Endpoints ---
@app.get("/")
async def root():
    return {"status": "running", "service": "Social Media Compliance Backend"}

@app.get("/health/live", include_in_schema=False)
async def health_live():
    return {"status": "ok"}

@app.get("/health/ready", include_in_schema=False)
async def health_ready():
    try:
        await asyncio.wait_for(fetch_one("SELECT 1 AS ready"), timeout=2.0)
    except Exception as exc:
        logger.warning("Readiness check failed", extra={"event": "readiness_check_failed", "error_type": type(exc).__name__})
        return JSONResponse(status_code=503, content={"status": "unavailable"})
    return {"status": "ready"}

@app.get("/api/integration-status")
@app.get("/api/integration/status")
async def integration_status():
    db_ok = True
    try:
        await asyncio.wait_for(fetch_one("SELECT 1 AS ready"), timeout=2.0)
    except Exception:
        db_ok = False

    return {
        "status": "healthy" if db_ok else "degraded",
        "services": [
            {
                "id": "database_pool",
                "name": "PostgreSQL Connection Pool",
                "state": "CONNECTED" if db_ok else "DISCONNECTED",
                "status": "online" if db_ok else "offline",
                "description": "Asyncpg connection pool with Tier 1 in-memory caching",
                "mode": "Active Pool",
                "failure_count": 0 if db_ok else 1,
            },
            {
                "id": "realtime_sse",
                "name": "Real-time Notification Stream (SSE)",
                "state": "STREAMING",
                "status": "online",
                "description": "Server-Sent Events with 30s zero-DB keep-alive heartbeat",
                "mode": "Live Stream",
                "failure_count": 0,
            },
        ],
    }


# --- Routers ---
authenticated = [Depends(get_current_user_id_dependency)]

app.include_router(message_hub_router, prefix="/api/message-hub", tags=["Message Hub"], dependencies=authenticated)
app.include_router(auth_router, prefix="/api", tags=["Authentication"])
app.include_router(rbac_router, prefix="/api", tags=["RBAC & Configuration"])
app.include_router(search_router, prefix="/api/search", tags=["Global Search"], dependencies=authenticated)
app.include_router(notification_router, prefix="/api", tags=["Notifications"], dependencies=authenticated)
app.include_router(teams_bot_router, prefix="/api", tags=["Microsoft Teams"])
app.include_router(teams_router, prefix="/api", tags=["Microsoft Teams"], dependencies=authenticated)
app.include_router(log_router, prefix="/api", tags=["Logs"])
app.include_router(observability_router, prefix="/api", tags=["Observability"])
app.include_router(graph_router, prefix="/api", tags=["Microsoft Graph"], dependencies=authenticated)
app.include_router(oauth_router, prefix="/api", tags=["OAuth & Integrations"])
app.include_router(compliance_router, prefix="/api", tags=["Compliance Rules & Governance"])
app.include_router(script_review_router, prefix="/api", tags=["Script Review"])
app.include_router(script_batch_router, prefix="/api", tags=["Script Batches"])
app.include_router(script_doc_router, prefix="/api", tags=["Script Documents"])
app.include_router(submission_router, prefix="/api", tags=["Content Submissions"])
app.include_router(source_routers, prefix="/api/submissions", tags=["Content Submissions"])
app.include_router(ai_status_router, prefix="/api", tags=["AI Status"], dependencies=authenticated)
app.include_router(chat_router, prefix="/api", tags=["AI Chat"])
app.include_router(approval_path_router, prefix="/api", tags=["Approval Paths"])
app.include_router(context_router, prefix="/api", tags=["Messaging Context"])
app.include_router(listening_router, prefix="/api", tags=["Listening Topics"])
app.include_router(performance_router, prefix="/api", tags=["Post Performance"])
app.include_router(canva_router, prefix="/api", tags=["Canva"])
app.include_router(vector_store_router, prefix="/api", tags=["Vector Store"])
app.include_router(sentiment_router, prefix="/api", tags=["Sentiment Quality"])

# --- Session Middleware ---
app.add_middleware(
    SessionMiddleware,
    secret_key=require_secret("SESSION_SECRET"),
    same_site="lax",
    https_only=os.getenv("SESSION_COOKIE_SECURE", "true" if _is_prod else "false").lower() in ("true", "1", "yes"),
    max_age=3600,
)

# --- Rate Limiting Middleware ---
_RATE_LIMIT_WINDOW_SECONDS = int(os.getenv("RATE_LIMIT_WINDOW_SECONDS", "60"))
_RATE_LIMIT_DEFAULT = int(os.getenv("RATE_LIMIT_REQUESTS_PER_WINDOW", "1200"))
_RATE_LIMIT_IP_CEILING = int(os.getenv("RATE_LIMIT_IP_CEILING_PER_WINDOW", "6000"))
_RATE_LIMIT_AUTH = int(os.getenv("RATE_LIMIT_AUTH_PER_WINDOW", "60"))
_RATE_LIMIT_ENABLED = _env_flag("RATE_LIMIT_ENABLED", "true")

_SENSITIVE_PREFIXES = (
    "/api/auth/microsoft/callback",
    "/api/auth/login",
    "/api/login",
    "/api/token",
)

_rate_buckets: dict[tuple[str, str], list[float]] = defaultdict(list)
_RATE_SWEEP_EVERY = 1000
_rate_requests_since_sweep = 0

def _sweep_rate_buckets(cutoff: float) -> None:
    for key in [k for k, hits in _rate_buckets.items() if not any(t > cutoff for t in hits)]:
        del _rate_buckets[key]

def _client_ip(request: Request) -> str:
    forwarded = request.headers.get("X-Forwarded-For", "")
    if forwarded and _env_flag("TRUST_FORWARDED_FOR", "false"):
        return forwarded.split(",")[0].strip()
    return request.client.host if request.client else "unknown"

def _session_identity(request: Request) -> Optional[str]:
    token = ""
    auth_header = request.headers.get("Authorization", "")
    if auth_header[:7].lower() == "bearer ":
        token = auth_header[7:].strip()
    if not token:
        from services.shared.auth.auth_service import AUTH_COOKIE_NAME
        token = request.cookies.get(AUTH_COOKIE_NAME, "") or request.cookies.get("zenatech_access_token", "")
    if not token:
        return None
    return "session:" + hashlib.sha256(token.encode("utf-8", "replace")).hexdigest()[:32]

@app.middleware("http")
async def rate_limit(request: Request, call_next):
    if not _RATE_LIMIT_ENABLED or request.method == "OPTIONS":
        return await call_next(request)

    path = request.url.path
    if path.startswith("/health"):
        return await call_next(request)

    source_ip = _client_ip(request)
    identity = _session_identity(request)

    if path.startswith(_SENSITIVE_PREFIXES):
        checks = [(("ip:" + source_ip, "auth"), _RATE_LIMIT_AUTH)]
    elif identity:
        checks = [
            ((identity, "session"), _RATE_LIMIT_DEFAULT),
            (("ip:" + source_ip, "ceiling"), _RATE_LIMIT_IP_CEILING),
        ]
    else:
        checks = [(("ip:" + source_ip, "anon"), _RATE_LIMIT_DEFAULT)]

    now = time.monotonic()
    cutoff = now - _RATE_LIMIT_WINDOW_SECONDS

    global _rate_requests_since_sweep
    _rate_requests_since_sweep += 1
    if _rate_requests_since_sweep >= _RATE_SWEEP_EVERY:
        _rate_requests_since_sweep = 0
        _sweep_rate_buckets(cutoff)

    exceeded = None
    for bucket_key, budget in checks:
        hits = _rate_buckets[bucket_key]
        hits[:] = [t for t in hits if t > cutoff]
        if len(hits) >= budget:
            exceeded = (bucket_key, budget, hits[0])
            break

    if exceeded is None:
        for bucket_key, _ in checks:
            _rate_buckets[bucket_key].append(now)

    if exceeded is not None:
        bucket_key, budget, oldest_hit = exceeded
        retry_after = max(1, int(oldest_hit - cutoff))
        logger.warning(
            "Rate limit exceeded",
            extra={
                "event": "rate_limit_exceeded",
                "path": path,
                "scope": bucket_key[1],
                "budget": budget,
                "window_seconds": _RATE_LIMIT_WINDOW_SECONDS,
            },
        )
        return JSONResponse(
            status_code=429,
            content={"detail": "Too many requests. Please slow down."},
            headers={"Retry-After": str(retry_after)},
        )

    return await call_next(request)


# --- Security Headers Middleware ---
@app.middleware("http")
async def security_headers(request: Request, call_next):
    response = await call_next(request)
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("X-Frame-Options", "DENY")
    response.headers.setdefault("Referrer-Policy", "no-referrer")
    response.headers.setdefault("Permissions-Policy", "camera=(), microphone=(), geolocation=()")
    response.headers.setdefault("Content-Security-Policy", CONTENT_SECURITY_POLICY)
    if _HSTS_ENABLED:
        response.headers.setdefault("Strict-Transport-Security", "max-age=31536000; includeSubDomains")
    return response


# --- Request Observability & Tracing Middleware ---
_REQUEST_ID_PATTERN = re.compile(r"^[A-Za-z0-9._:-]{1,128}$")

def _slow_request_threshold_ms() -> int:
    try:
        configured = int(os.getenv("SLOW_REQUEST_MS", "2000"))
    except (TypeError, ValueError):
        configured = 2000
    return max(100, configured)

SLOW_REQUEST_THRESHOLD_MS = _slow_request_threshold_ms()

def _should_log_request_completion(path: str, status_code: int, duration_ms: float) -> bool:
    if path in {"/health/live", "/health/ready"}:
        return False
    return status_code >= 400 or duration_ms >= SLOW_REQUEST_THRESHOLD_MS

@app.middleware("http")
async def request_observability(request: Request, call_next):
    supplied_request_id = request.headers.get("x-request-id", "")
    request_id = (
        supplied_request_id
        if _REQUEST_ID_PATTERN.fullmatch(supplied_request_id)
        else str(uuid4())
    )
    token = request_id_ctx.set(request_id)
    started = time.perf_counter()
    client_ip = request.client.host if request.client else None
    trace_id = request.headers.get("x-amzn-trace-id", "")[:256] or None
    response = None
    unhandled = False
    try:
        response = await call_next(request)
    except (asyncio.CancelledError, ConnectionResetError):
        request_id_ctx.reset(token)
        return JSONResponse(status_code=499, content={"detail": "Client closed request"})
    except Exception:
        unhandled = True
        duration_ms = round((time.perf_counter() - started) * 1000, 2)
        logger.exception(
            "Unhandled request exception",
            extra={
                "event": "unhandled_request_exception",
                "request_id": request_id,
                "trace_id": trace_id,
                "method": request.method,
                "path": request.url.path,
                "status_code": 500,
                "duration_ms": duration_ms,
                "client_ip": client_ip,
            },
        )
        response = JSONResponse(
            status_code=500,
            content={"detail": "Internal server error", "request_id": request_id},
        )
    finally:
        duration_ms = round((time.perf_counter() - started) * 1000, 2)

    response.headers["X-Request-ID"] = request_id
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("X-Frame-Options", "DENY")
    response.headers.setdefault("Referrer-Policy", "no-referrer")
    response.headers.setdefault("Permissions-Policy", "camera=(), microphone=(), geolocation=()")
    log_level = logging.WARNING if duration_ms >= SLOW_REQUEST_THRESHOLD_MS else logging.INFO
    if response.status_code >= 500 and not unhandled:
        log_level = logging.ERROR
    path = request.url.path
    if _should_log_request_completion(path, response.status_code, duration_ms):
        logger.log(
            log_level,
            "Request completed",
            extra={
                "event": "request_completed",
                "request_id": request_id,
                "trace_id": trace_id,
                "method": request.method,
                "path": path,
                "status_code": response.status_code,
                "duration_ms": duration_ms,
                "client_ip": client_ip,
            },
        )
    request_id_ctx.reset(token)
    return response

# --- CORS Configuration ---
cors_origins = [origin.strip() for origin in (os.getenv("CORS_ALLOWED_ORIGINS", "") or os.getenv("CORS_ORIGINS", "")).split(",") if origin.strip()]
if _is_prod and not cors_origins: raise RuntimeError("CORS_ALLOWED_ORIGINS must list explicit HTTPS origins when APP_ENV=production")
_cors_regex = None if _is_prod else r"^https?://(localhost|127\.0\.0\.1|192\.168\.\d+\.\d+|10\.\d+\.\d+\.\d+|172\.(?:1[6-9]|2\d|3[01])\.\d+\.\d+)(:\d+)?$"

app.add_middleware(
    CORSMiddleware,
    allow_origins=cors_origins,
    allow_origin_regex=_cors_regex,
    allow_credentials=True,
    allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
    allow_headers=["*"],
    expose_headers=["X-Request-ID"],
)

if __name__ == "__main__":
    mcp.run(
        transport="streamable-http",
        host=os.getenv("MCP_HOST", "0.0.0.0"),
        port=int(os.getenv("MCP_PORT", "8002")),
    )
