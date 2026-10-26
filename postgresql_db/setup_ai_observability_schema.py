"""AI observability and caching: trace spans for every AI step (model calls, embeddings, retrieval, tool calls,
guard checks, cache lookups), the prompt log of every model call, and the semantic cache of assistant answers.
See services/ai_observability.py and services/semantic_cache.py."""

import logging

from postgresql_db.database import get_pool

logger = logging.getLogger(__name__)


async def setup() -> None:
    pool = get_pool()
    async with pool.acquire() as conn:
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS ai_trace_spans (
                id UUID PRIMARY KEY,
                -- The request's X-Request-ID when the work ran inside a request, otherwise a fresh id.
                trace_id VARCHAR(128) NOT NULL,
                parent_id UUID,
                kind VARCHAR(30) NOT NULL,
                name VARCHAR(200) NOT NULL,
                status VARCHAR(20) NOT NULL,
                error_type VARCHAR(200),
                user_id UUID,
                started_at TIMESTAMPTZ NOT NULL,
                duration_ms DOUBLE PRECISION NOT NULL,
                attributes JSONB NOT NULL DEFAULT '{}'::jsonb
            );
            CREATE INDEX IF NOT EXISTS idx_ai_trace_spans_trace ON ai_trace_spans (trace_id);
            CREATE INDEX IF NOT EXISTS idx_ai_trace_spans_started ON ai_trace_spans (started_at DESC);
            CREATE INDEX IF NOT EXISTS idx_ai_trace_spans_kind ON ai_trace_spans (kind, name, started_at DESC);

            CREATE TABLE IF NOT EXISTS ai_prompt_logs (
                id UUID PRIMARY KEY,
                trace_id VARCHAR(128),
                span_id UUID,
                feature VARCHAR(40) NOT NULL,
                model VARCHAR(100),
                user_id UUID,
                submission_id UUID,
                -- PII-redacted and cut to AI_PROMPT_LOG_MAX_CHARS; NULL when AI_PROMPT_LOG_CONTENT=false.
                prompt TEXT,
                response TEXT,
                status VARCHAR(20) NOT NULL,
                error_type VARCHAR(200),
                latency_ms DOUBLE PRECISION,
                input_tokens INTEGER,
                output_tokens INTEGER,
                guard_verdict VARCHAR(10),
                guard_categories TEXT[] NOT NULL DEFAULT '{}',
                pii_redactions JSONB NOT NULL DEFAULT '{}'::jsonb,
                cache_hit BOOLEAN NOT NULL DEFAULT FALSE,
                created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
            );
            CREATE INDEX IF NOT EXISTS idx_ai_prompt_logs_created ON ai_prompt_logs (created_at DESC);
            CREATE INDEX IF NOT EXISTS idx_ai_prompt_logs_feature ON ai_prompt_logs (feature, created_at DESC);
            CREATE INDEX IF NOT EXISTS idx_ai_prompt_logs_trace ON ai_prompt_logs (trace_id);

            CREATE TABLE IF NOT EXISTS ai_semantic_cache (
                id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
                feature VARCHAR(40) NOT NULL,
                -- Who may share the answer (for the chat, a fingerprint of the asker's permissions and the rulebook).
                scope VARCHAR(128) NOT NULL,
                query_hash CHAR(64) NOT NULL,
                query_text TEXT NOT NULL,
                -- The numbers in the question: a hit needs exactly the same ones.
                numbers TEXT[] NOT NULL DEFAULT '{}',
                -- Unit-length embedding, so the dot product is the cosine similarity.
                embedding REAL[] NOT NULL,
                response TEXT NOT NULL,
                model VARCHAR(100),
                hits INTEGER NOT NULL DEFAULT 0,
                created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                last_hit_at TIMESTAMPTZ,
                expires_at TIMESTAMPTZ NOT NULL,
                UNIQUE (feature, scope, query_hash)
            );
            CREATE INDEX IF NOT EXISTS idx_ai_semantic_cache_lookup
                ON ai_semantic_cache (feature, scope, created_at DESC);
        """)
    logger.info("AI observability schema ready", extra={"event": "ai_observability_schema_ready"})
