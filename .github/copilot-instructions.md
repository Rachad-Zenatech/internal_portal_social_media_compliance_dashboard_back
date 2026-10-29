# GitHub Copilot Instructions

- Use FastAPI routers in `tools/`, services in `services/`, models in `models/`, and database helpers in `postgresql_db/database.py`.
- Synchronous database pool access: `pool = get_pool()`.
- Use the caching layer (`services.shared.cache_service`) for high-frequency queries and emit invalidation events on data modification.
- Never write SQL queries inside loops; batch multi-row operations using PostgreSQL `UNNEST`.
- Validate code with `wsl python3 -m py_compile <files>`.

<!-- project-structure:start -->
## Project Structure

```text
server.py             # FastAPI app: startup/shutdown, middleware, router registration
tools/                # HTTP routers (FastAPI APIRouter), grouped by feature
  shared/             # Auth, roles and permissions, logs, health/observability, search and social account OAuth connections
    auth_router.py, log_router.py, oauth_router.py
    observability.py, rbac_router.py, search.py
  ai/                 # Vector store admin: MongoDB connection, search index, review memory sync
    vector_store_router.py
  notifications/      # Notifications and messaging (SSE, RabbitMQ, MQTT, cross-portal events)
    notification_router.py
  compliance/         # Compliance review: submissions, rulebook checks, AI content analysis, review flow, Canva import, post metrics, social listening
    approval_path_router.py, canva_router.py, compliance_router.py
    context_router.py, listening_router.py, performance_router.py
    sentiment_router.py, submission_router.py
  microsoft_graph/    # Microsoft Graph (Entra users/org) client and background sync
    graph_router.py
services/             # Business logic, grouped by feature
  shared/             # Used by several features: auth, RBAC, caching, logging, audit, security
    audit_service.py, auth_service.py, cache_service.py
    logging_service.py, oauth_connection_service.py, rbac_service.py
    service_security_policy.py
  ai/                 # Gemini clients, assistant chat, guardrails, tracing, semantic cache, usage limits
    ai_guardrails.py, ai_observability.py, ai_observability_service.py
    ai_usage_service.py, assistant_chat_service.py, gemini_service.py
    model_router.py, semantic_cache.py, vector_store_service.py
  notifications/      # Notifications and messaging (SSE, RabbitMQ, MQTT, cross-portal events)
    ceo_event_publisher.py, email_service.py, mqtt_presence.py
    notification_service.py, rabbitmq_service.py
  compliance/         # Compliance review: submissions, rulebook checks, AI content analysis, review flow, Canva import, post metrics, social listening
    approval_path_service.py, canva_mock_render.py, canva_service.py
    caption_service.py, claim_checks.py, compliance_service.py
    context_service.py, document_text_service.py, flag_review_service.py
    hashtag_library.py, html_sanitizer.py, image_text_service.py
    listening_providers.py, performance_service.py, post_metrics_providers.py
    post_signals.py, public_mood_service.py, reference_index_service.py
    review_deadline_service.py, review_memory_service.py, sentiment_service.py
    submission_service.py, transcription_service.py, video_job_service.py
  microsoft_graph/    # Microsoft Graph (Entra users/org) client and background sync
    graph_service.py, graph_sync_job.py
models/               # Pydantic request/response models, grouped by feature
  shared/             # Used by several features: users, roles, permissions, common types
    oauth_connection_model.py, rbac_model.py
  notifications/      # Notifications and messaging (SSE, RabbitMQ, MQTT, cross-portal events)
    notification_model.py
  compliance/         # Compliance review: submissions, rulebook checks, AI content analysis, review flow, Canva import, post metrics, social listening
    approval_path_model.py, canva_model.py, caption_model.py
    compliance_model.py, context_model.py, flag_review_model.py
    performance_model.py, public_mood_model.py, sentiment_model.py
    submission_model.py
mcp_tools/            # MCP server: registry, middleware, prompts; tools grouped by feature
    __init__.py (registry), middleware.py, prompts.py
  shared/             # Tools for several features: permissions and account lookups
    permissions.py
  compliance/         # Rulebook and Messaging Context search (vector search over reference material)
    reference.py
postgresql_db/        # database.py (asyncpg pool), schema_metadata.py, table scripts in schemas/
  schemas/            # setup_<feature>_schema.py: CREATE TABLE / migrations per feature
    notifications_schema.py, setup_ai_observability_schema.py, setup_compliance_schema.py
    setup_context_schema.py, setup_graph_sync_schema.py, setup_listening_schema.py
    setup_login_codes_schema.py, setup_pbac_schema.py, setup_performance_schema.py
    setup_social_media_schema.py, setup_submissions_schema.py
alembic/              # Database migrations
scripts/              # Dev/ops scripts (seed, reset, pull prod data)
tests/                # Tests
docs/                 # Longer design and integration docs
```

### Where new code goes

* **New feature `<x>`:** logic in `services/<x>/`, endpoints in `tools/<x>/<x>_router.py`, models in `models/<x>/`, MCP tools in `mcp_tools/<x>/` (register them in `mcp_tools/__init__.py`), tables in `postgresql_db/schemas/setup_<x>_schema.py` (run with `python -m postgresql_db.schemas.setup_<x>_schema`).
* **Code two or more features use** goes in the `shared/` folder of that layer (or `ai/`, `notifications/` when it fits there). A feature folder must not import another feature's folder; move the common piece to `shared/` instead.
* **No loose files** at the root of `tools/`, `services/` or `models/`. In `mcp_tools/` only the registry (`__init__.py`), `middleware.py` and `prompts.py` sit at the root.
* **Imports are absolute** (`from services.shared.auth_service import ...`). Feature folders are namespace packages with no `__init__.py`, like the rest of the repo.
* **When you move a module,** update every import, `mock.patch("...")` string, `python -m` command (CI, scripts) and doc path, and fix any `__file__`-relative path in it (one more folder deep means one more `dirname`).
<!-- project-structure:end -->
