# Social Media Compliance Dashboard Backend

Backend service for the ZenaTech Social Media Compliance internal portal. Built on **FastAPI**, **PostgreSQL** (`asyncpg`), **FastMCP**, **Microsoft Entra ID OAuth 2.0**, **PBAC (Permission-Based Access Control)**, and zero-CPU **Server-Sent Events (SSE)**, with a guarded **Gemini AI pipeline** (RAG, input screening, PII redaction, semantic cache, model routing, tracing, prompt logs and evaluation). See [AI Pipeline](#ai-pipeline).

---

## System Architecture Diagram

```mermaid
flowchart TD
    subgraph ClientLayer [Client Consumers]
        FrontApp[Social Media Compliance Frontend :5179]
        MCPClient[MCP AI Agents & Inspectors]
        ThirdParty[External REST Integrations]
    end

    subgraph APIGateway [FastAPI HTTP Engine :8009]
        CORS[CORS & Security Headers Middleware]
        RateLimit[In-Memory Sliding-Window Rate Limiter]
        AuthRouter[Auth & Microsoft SSO Router]
        RBACRouter[Users, Roles & PBAC Router]
        LogRouter[System & Audit Log Router]
        NotifRouter[Real-Time SSE Event Stream]
        ObsRouter[Health Checks & Telemetry]
        MCPEndpoint[FastMCP Protocol Server /mcp]
    end

    subgraph CoreServices [Service Layer]
        AuthService[Auth & Token Lifecycle Service]
        RBACService[Granular PBAC Authorization Engine]
        CacheService[L1 In-Memory Cache + PostgreSQL NOTIFY]
        AuditService[Audit Log & Action Differ]
        NotificationService[SSE Event Broadcaster & Queue]
        GeminiService[Google Gemini AI Integration]
    end

    subgraph DataPersistence [Database & State Layer]
        PG[(PostgreSQL asyncpg Connection Pool)]
        Alembic[Alembic Database Migration Engine]
    end

    FrontApp -->|REST API with HttpOnly Cookies| APIGateway
    FrontApp -->|Persistent EventSource Stream| NotifRouter
    MCPClient -->|Streamable HTTP /mcp| MCPEndpoint
    ThirdParty -->|Bearer Token API| APIGateway

    APIGateway --> RateLimit
    RateLimit --> CoreServices
    CoreServices --> CacheService
    CoreServices --> PG
    CacheService <-->|NOTIFY / LISTEN| PG
```

---

## Technologies & System Specifications

| Category | Technology | Description |
| :--- | :--- | :--- |
| **Framework & Engine** | [FastAPI](https://fastapi.tiangolo.com/), [Uvicorn](https://www.uvicorn.org/) | High-concurrency ASGI web framework running on port `8009` |
| **Language** | Python 3.12+ | Strongly typed with Pydantic v2 schemas and async event loops |
| **Database & Pooling** | [PostgreSQL](https://www.postgresql.org/), [asyncpg](https://github.com/MagicStack/asyncpg) | Direct binary wire protocol, synchronous pool manager `get_pool()`, atomic `UNNEST` batch queries |
| **Database Migrations**| [Alembic](https://alembic.sqlalchemy.org/) | Declarative schema revision management |
| **Model Context Protocol**| [FastMCP](https://github.com/jlowin/fastmcp), `mcp` SDK | Streamable HTTP endpoint (`/mcp/`) exposing role-checked tools to MCP clients and to the assistant chat |
| **Real-Time Streaming**| [Server-Sent Events (SSE)](https://developer.mozilla.org/en-US/docs/Web/API/Server-sent_events) | Event-driven async queues with 30s lightweight keepalive pings and automatic reconnect |
| **Authentication & SSO**| `Authlib`, `PyJWT`, `passlib`, `bcrypt` | Microsoft Entra ID (Azure AD) OAuth 2.0 PKCE and secure HttpOnly cookie sessions |
| **Authorization (PBAC)**| Custom PBAC Matrix | User -> Roles -> Group Actions (`VIEW`, `CREATE`, `EDIT`, `DELETE`) & Route Method Policies |
| **AI Integration** | [Google GenAI SDK](https://github.com/googleapis/python-genai) (`google-genai`) | Sentiment and claim analysis, caption suggestions, transcription, public mood summaries, assistant chat with tool calling |
| **Vector Store (RAG)** | MongoDB Atlas Vector Search (`motor`), Gemini embeddings | Rulebook, Messaging Context, past review decisions and listening topics, retrieved by meaning at query time |
| **AI Guardrails** | `services/ai/ai_guardrails.py` | Jailbreak / prompt-injection input classifier and PII redaction of AI output |
| **AI Observability** | `services/ai/ai_observability.py` | A trace span for every model call, embedding, retrieval, tool call, guard check and cache lookup, plus prompt logs |
| **Rate Limiting & Security**| Sliding Window Counter | Per-IP and per-user window throttling protecting sensitive auth routes |

---

<!-- project-structure:start -->
## Project Structure

```text
server.py             # FastAPI app: startup/shutdown, middleware, router registration
tools/                # HTTP routers (FastAPI APIRouter), one file per feature
services/             # Business logic, grouped by feature (below)
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
models/               # Pydantic request/response models
postgresql_db/        # database.py (asyncpg pool) and setup_*_schema.py table scripts
mcp_tools/            # MCP tools and prompts (also used by the AI assistant via run_tool)
alembic/              # Database migrations
scripts/              # Dev/ops scripts (seed, reset, pull prod data)
tests/                # Tests
docs/                 # Longer design and integration docs
```

### Where new code goes

* **New feature `<x>`:** logic in `services/<x>/`, endpoints in `tools/<x>_router.py`, models in `models/`, tables in `postgresql_db/setup_<x>_schema.py`.
* **Code two or more features use** goes in `services/shared/` (or `services/ai/`, `services/notifications/` when it fits there). A feature folder must not import another feature's folder; move the common piece to `services/shared/` instead.
* **Never add files at the root of `services/`.** Every module lives in a feature or shared folder.
* **Imports are absolute** (`from services.shared.auth_service import ...`). The folders are namespace packages with no `__init__.py`, like the rest of the repo.
* **When you move a module,** update every import, `mock.patch("...")` string and doc path, and fix any `__file__`-relative path in it (one more folder deep means one more `dirname`).
<!-- project-structure:end -->

---

## API Endpoints Overview

| Method | Endpoint | Description | Auth Required |
| :--- | :--- | :--- | :--- |
| `GET` | `/health/live` | Container liveness probe | No |
| `POST` | `/api/auth/microsoft/login` | Initiate Microsoft Entra SSO | No |
| `GET` | `/api/auth/me` | Current session user & permission list | Yes |
| `POST` | `/api/auth/logout` | Clear session cookie | Yes |
| `GET` | `/api/configuration/users` | List portal users with pagination & search | `USERS:VIEW` |
| `POST` | `/api/configuration/users` | Create new portal user | `USERS:CREATE` |
| `GET` | `/api/configuration/roles` | List all configured roles and hierarchies | `ROLES:VIEW` |
| `PUT` | `/api/configuration/roles/{id}` | Update role permissions | `ROLES:EDIT` |
| `GET` | `/api/notifications/stream` | Real-time SSE event stream | Yes |
| `GET` | `/api/logs` | Query audit log trail and system events | `LOGS:VIEW` |
| `POST` | `/ai/chat` | Assistant chat (SSE stream) | Yes |
| `POST` | `/mcp/` | FastMCP tool endpoint (Streamable HTTP); tools listed and checked per role | Bearer access token |
| `GET` | `/api/observability/ai/summary` | Calls, errors, p50/p95 latency, tokens per feature and model; cache hits; guard verdicts | `SYSTEM_LOGS:VIEW` |
| `GET` | `/api/observability/ai/traces` | Recent AI traces (filter by `kind`, `status`) | `SYSTEM_LOGS:VIEW` |
| `GET` | `/api/observability/ai/traces/{trace_id}` | Every span and prompt log of one trace | `SYSTEM_LOGS:VIEW` |
| `GET` | `/api/observability/ai/prompt-logs` | Prompts and responses, PII-redacted (filter by `feature`, `status`, `guard_verdict`) | `SYSTEM_LOGS:VIEW` |
| `GET` | `/api/observability/usage/tokens` | Token totals per feature and model | `SYSTEM_LOGS:VIEW` |

---

## Local Development & Setup

### 1. Python Environment Setup
```bash
python3 -m venv venv
# On Windows:
venv\Scripts\activate
# On Linux / WSL:
source venv/bin/activate
```

### 2. Install Dependencies
```bash
pip install -r requirements.txt
```

### 3. Environment Variables (.env)
```env
PORT=8009
FRONTEND_URL=http://localhost:5179
DATABASE_URL=postgresql://postgres:postgres@localhost:3008/social_media_compliance_dev
DATABASE_SSL=false
SESSION_SECRET=your-32-byte-secret-key-goes-here
MICROSOFT_CLIENT_ID=your-azure-ad-client-id
MICROSOFT_CLIENT_SECRET=your-azure-ad-client-secret
MICROSOFT_TENANT_ID=common
GEMINI_API_KEY=your-gemini-api-key
MCP_PORT=7006
# Optional: the vector store for RAG (off when unset)
MONGODB_URI=mongodb+srv://...
```
The AI pipeline's settings (all optional, with defaults) are listed under [AI Pipeline > Configuration](#configuration).

### 4. Run Development Server
```bash
uvicorn server:app --host 0.0.0.0 --port 8009 --reload
```
Open API docs at `http://localhost:8009/docs`.

### 5. Run the Tests
```bash
pip install -r requirements-dev.txt
pytest -q
```
- Tests follow the finance backend's (`internal_portal`) format: `unittest` test classes in `tests/test_*.py`, with the database, Gemini and MongoDB replaced by `unittest.mock` (`patch`, `AsyncMock`). Nothing needs to be running, and no real data or AI quota is touched.
- Async code is tested with `unittest.IsolatedAsyncioTestCase`; small builder helpers (e.g. `submission_row()`, `rule()`) sit at the top of each file.
- CI runs the suite on every pull request to `main`; a failing test blocks the build.

The AI safety eval runs in CI too, and needs nothing running:
```bash
python scripts/ai_safety_eval.py            # failures only; exits 1 if a target is missed
python scripts/ai_safety_eval.py --verbose  # every case
```


## AI Pipeline

Every Gemini call is traced and prompt-logged. The assistant chat uses every layer below. Of the content
analyses, captions, public mood and transcription are also model-routed. Sentiment and image text reading keep
their own model chains, and sentiment also records input screening of the post's text.

```mermaid
flowchart LR
    Q[Chat message] --> G{Input screening}
    G -->|BLOCK| R[Refused, logged]
    G -->|ALLOW / FLAG| C{Semantic cache}
    C -->|hit| A[Answer]
    C -->|miss| U[AI quota reserved] --> M[Model router] --> L[Gemini]
    L -->|tool call| T[MCP tool: RBAC check] --> V[(Vector store / rulebook)]
    T --> L
    L --> P[PII redaction] --> A
    L -.-> O[(Trace spans + prompt logs)]
    T -.-> O
```

### RAG (retrieval at query time)
- `vector_store_service` embeds text with Gemini (`gemini-embedding-2`) into MongoDB Atlas Vector Search. It is off until `MONGODB_URI` is set, and every caller carries on without it.
- What's indexed:
  - the compliance rulebook and Messaging Context (brand voice, facts, platform norms, audience profiles), via `reference_index_service`, re-synced after every change and on start;
  - past review decisions (`review_memory_service`);
  - listening topics (`public_mood_service`).
- At query time:
  - the assistant calls `search_reference_material_tool` to fetch the passages that answer a question;
  - sentiment analysis pulls the most similar past reviews as precedent.

### MCP server and RBAC
- The tools in `mcp_tools/` are served at `/mcp/` and are also offered to the assistant chat through Gemini function calling. Both paths use the same RBAC check, tracing and redaction.
- Identity is the portal's own access token, issued after Microsoft Entra ID sign-in. MCP clients send it as `Authorization: Bearer <token>`, and it goes through the same checks as the API (expiry, logout, inactivity).
- Each tool has a permission code. A role only sees and can call the tools it has been granted (`role_mcp_tool_permissions`); super admins get every tool.

  | Tool | Permission code |
  | :--- | :--- |
  | `search_reference_material_tool` | `REFERENCE_SEARCH` |
  | `list_compliance_rules_tool` | `COMPLIANCE_RULES_VIEW` |
  | `list_my_accessible_tools_tool` | `PERMISSIONS_VIEW` |

  New tools are added to the `mcp_tools` table on start. Grant them to roles in the RBAC configuration.

### Input screening (jailbreak / prompt-injection classifier)
- `ai_guardrails.screen_input` scores text against weighted pattern categories:
  - instruction override and system prompt extraction;
  - role hijack and safety bypass;
  - forced verdicts (e.g. "return NO_CONCERNS");
  - fake role or delimiter markers;
  - privilege escalation;
  - hidden or encoded payloads.
- Verdicts are `ALLOW`, `FLAG` (≥ 0.4) and `BLOCK` (≥ 0.7).
- The chat refuses `BLOCK` before calling Gemini or using quota. Both the message and the person's earlier turns are screened.
- Sentiment analysis records the verdict for the post's own text but always runs: a post that tries to steer the review is itself something reviewers should see.
- It is a pattern classifier: fast and free, but it only catches phrasings it knows. When one gets through, add it to `scripts/ai_safety_eval.py` and to the patterns.

### PII redaction on output
- `ai_guardrails.redact_pii` replaces these with `[REDACTED_<KIND>]`:
  - emails, phone numbers and US SSNs;
  - payment card numbers (Luhn-checked) and IBANs;
  - IP addresses;
  - API keys, JWTs and private keys.
- Where it applies:
  - the streamed chat answer, through `StreamRedactor`, so a match split across chunks is still caught;
  - every tool result;
  - every stored prompt log.
- `PII_ALLOWED_EMAIL_DOMAINS` (e.g. `zenatech.com`) keeps company addresses readable.

### Semantic cache
- Chat questions asked without earlier turns are cached in `ai_semantic_cache`. A repeated question is answered without calling Gemini and doesn't count against AI quota.
- A hit needs one of these, plus exactly the same numbers in both questions, so "30%" never reuses an answer about "300%":
  - an exact match on the normalised question, or
  - cosine similarity ≥ 0.95 between Gemini embeddings (256 dimensions), compared in SQL.
- Scope: answers are shared only between people with the same tool permissions and the same rulebook version.
- Expiry: 24 hours, and the chat's entries are cleared when reference material changes.
- Content analyses are never cached by meaning: two posts that read alike can still differ in a figure or a claim.

### Model routing
- `model_router` sorts models into tiers: `LIGHT` (flash-lite first), `STANDARD` (flash) and `HEAVY` (a stronger model when configured).
- Each feature has a base tier (chat `LIGHT`; captions, public mood and transcription `STANDARD`). A request moves up for attachments, a long prompt or high stakes.
- The rest of the model list follows as fallback for quota, overload or retired models.
- Sentiment analysis and image text reading keep their own chains (`SENTIMENT_MODELS`, `SENTIMENT_MODEL_HIGH_STAKES`), because their cached results are keyed by model.

### Tracing and prompt logs
- `ai_observability.span()` records a span for every model call, embedding, vector search, tool call (from `/mcp` or the chat), guard check and cache lookup.
- Each span has its duration, status (`OK`, `ERROR`, `DENIED`, `CANCELLED`) and attributes: model, tokens, route tier, guard verdict, result counts.
- Spans nest under one `trace_id`, which is the request's `X-Request-ID`, so a whole chat turn can be read back with `/api/observability/ai/traces/{trace_id}`.
- Every model call is also written to `ai_prompt_logs`: prompt, response, model, latency, tokens, guard verdict, cache hit.
- Prompt and response text is PII-redacted and cut to 20,000 characters; `AI_PROMPT_LOG_CONTENT=false` keeps metadata only.
- Writes are batched off the request path: an in-memory queue feeds a background writer that waits on it and inserts each burst with one `UNNEST` statement.
- Retention: 30 days.

### Evaluation suite
- `scripts/ai_safety_eval.py` (offline, runs in CI) checks the following against labelled cases, with recall and precision targets for the classifier:
  - input screening;
  - PII redaction, whole and streamed;
  - model routing;
  - the cache's number guard.
- `scripts/sentiment_eval.py` runs the rulebook's own examples and reviewers' saved cases through the real sentiment analysis (calls Gemini).

### Configuration
| Variable | Default | Purpose |
| :--- | :--- | :--- |
| `AI_GUARD_FLAG_THRESHOLD` / `AI_GUARD_BLOCK_THRESHOLD` | `0.4` / `0.7` | Input screening score cut-offs |
| `PII_ALLOWED_EMAIL_DOMAINS` | (none) | Email domains left unredacted, comma-separated |
| `AI_SEMANTIC_CACHE_ENABLED` | `true` | Turn the semantic cache off |
| `AI_SEMANTIC_CACHE_THRESHOLD` | `0.95` | Minimum cosine similarity for a hit |
| `AI_SEMANTIC_CACHE_TTL_HOURS` | `24` | How long an answer is reused |
| `AI_SEMANTIC_CACHE_DIMENSIONS` | `256` | Embedding size for cache entries |
| `AI_ROUTING_ENABLED` | `true` | `false` uses the plain fallback order for everything |
| `AI_ROUTE_TIER_LIGHT` / `_STANDARD` / `_HEAVY` | see `model_router.py` | Models per tier, comma-separated, in order |
| `AI_ROUTE_<FEATURE>` | per feature | Base tier for a feature (`CHAT`, `CAPTION`, `PUBLIC_MOOD`, `TRANSCRIPTION`) |
| `AI_PROMPT_LOG_CONTENT` | `true` | `false` stores prompt metadata only |
| `AI_PROMPT_LOG_MAX_CHARS` | `20000` | Characters kept per prompt and response |
| `AI_TRACE_RETENTION_DAYS` / `AI_PROMPT_LOG_RETENTION_DAYS` | `30` / `30` | Retention |
| `AI_LIMIT_<FEATURE>_PER_USER_DAY` etc. | see `ai_usage_service.py` | Daily AI quota per person, submission and company |


## Local Database (Docker), DataGrip & Migrations

Each project in the ZenaTech ecosystem runs an isolated local PostgreSQL container mapped to a dedicated 3000-series host port to prevent port collisions.

### 1. Local Database Configuration
* **Container Name**: `social_media_db_local`
* **Host Port**: `localhost:3008` (mapped to internal PostgreSQL 5432)
* **Database Name**: `social_media_compliance_dev`
* **User / Password**: `postgres` / `postgres`
* **Connection String**: `postgresql://postgres:postgres@localhost:3008/social_media_compliance_dev`

### 2. Managing the Local Database
```bash
# Start local database container
docker compose up -d db

# Stop local database container
docker compose down
```
### 3. Connecting in DataGrip
1. Create a new **PostgreSQL** Data Source in DataGrip.
2. Settings:
   * **Host**: `localhost`
   * **Port**: `3008`
   * **Database**: `social_media_compliance_dev`
   * **User**: `postgres`
   * **Password**: `postgres`
3. Click **Test Connection** and apply.

### 4. Syncing Latest Live Data from AWS RDS (Optional)
To pull a copy of real live records from AWS RDS into your local Docker DB for realistic development:
```powershell
.\scripts\pull_prod_to_local.ps1
```
* Safely downloads an RDS snapshot and restores it into `localhost:3005`.
* Read-only pull: live AWS RDS remains untouched and safe.

### 5. Creating Schema Changes & Migrations (PR Workflow)
When making table or column changes on a new branch:

1. **Create and Switch to Your New Feature Branch**:
   ```bash
   git checkout -b feature/your-feature-name
   ```
   *(The Git `post-checkout` hook in `.git/hooks/post-checkout` automatically triggers to ensure your local database is synced and ready).*

2. **Sync Live Data into Local DataGrip (Optional / Recommended)**:
   ```powershell
   .\scripts\pull_prod_to_local.ps1
   ```
   *(Safely pulls real current rows into your local Docker DB on port 3005 so you can build and test queries with actual data).*

3. **Update Code Schema**:
   Edit `postgresql_db/schema_metadata.py` to add or modify columns/tables.

4. **Auto-Generate Migration Diff**:
   ```bash
   alembic revision --autogenerate -m "describe changes"
   ```
   Alembic inspects your local database and writes a versioned migration in `alembic/versions/` containing **only the differences**.

5. **Apply & Test Locally**:
   ```bash
   alembic upgrade head
   ```
   Verify your changes and tables in DataGrip on `127.0.0.1:3005` with your test data.

6. **Submit in PR**:
   Commit and push:
   * `postgresql_db/schema_metadata.py`
   * `alembic/versions/<revision_id>_*.py`
   *(Local test data stays on your machine; only the table structure diff is merged to production!)*

### 6. Disposable Database Reset
To tear down and recreate your local container from scratch to match the current branch:
```powershell
.\scripts\reset_local_db.ps1
```
* A Git `post-checkout` hook is installed in `.git/hooks/post-checkout` to trigger this automatically on branch switches.
* Built-in safety guards immediately block resets if `DATABASE_URL` points to a remote AWS RDS host.



