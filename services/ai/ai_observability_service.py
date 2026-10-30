"""Reading AI traces and prompt logs back for the observability API (writing is in ai_observability)."""

import json
from typing import Any, Dict, List, Optional

from postgresql_db.database import fetch_all, fetch_one


def _attrs(value: Any) -> Any:
    return json.loads(value) if isinstance(value, str) else value


async def usage_summary(hours: int) -> Dict[str, Any]:
    rows = await fetch_all(
        """
        SELECT feature, model, count(*) AS calls,
               count(*) FILTER (WHERE status = 'ERROR') AS errors,
               count(*) FILTER (WHERE cache_hit) AS cache_hits,
               coalesce(sum(input_tokens), 0) AS input_tokens,
               coalesce(sum(output_tokens), 0) AS output_tokens,
               round(avg(latency_ms)::numeric, 1) AS avg_latency_ms
        FROM ai_prompt_logs
        WHERE created_at > NOW() - make_interval(hours => $1)
        GROUP BY feature, model ORDER BY calls DESC
        """,
        hours,
    )
    return {
        "hours": hours,
        "input_tokens": sum(r["input_tokens"] for r in rows),
        "output_tokens": sum(r["output_tokens"] for r in rows),
        "by_feature_model": rows,
    }


async def summary(hours: int) -> Dict[str, Any]:
    models = await fetch_all(
        """
        SELECT feature, model, count(*) AS calls,
               count(*) FILTER (WHERE status = 'ERROR') AS errors,
               count(*) FILTER (WHERE status = 'BLOCKED') AS blocked,
               count(*) FILTER (WHERE cache_hit) AS cache_hits,
               percentile_cont(0.5) WITHIN GROUP (ORDER BY latency_ms) AS p50_latency_ms,
               percentile_cont(0.95) WITHIN GROUP (ORDER BY latency_ms) AS p95_latency_ms,
               coalesce(sum(input_tokens), 0) AS input_tokens,
               coalesce(sum(output_tokens), 0) AS output_tokens
        FROM ai_prompt_logs
        WHERE created_at > NOW() - make_interval(hours => $1)
        GROUP BY feature, model ORDER BY feature, calls DESC
        """,
        hours,
    )
    spans = await fetch_all(
        """
        SELECT kind, name, count(*) AS calls,
               count(*) FILTER (WHERE status NOT IN ('OK')) AS not_ok,
               percentile_cont(0.5) WITHIN GROUP (ORDER BY duration_ms) AS p50_ms,
               percentile_cont(0.95) WITHIN GROUP (ORDER BY duration_ms) AS p95_ms
        FROM ai_trace_spans
        WHERE started_at > NOW() - make_interval(hours => $1) AND kind IN ('tool', 'retrieval', 'embedding', 'cache')
        GROUP BY kind, name ORDER BY kind, calls DESC
        """,
        hours,
    )
    guard = await fetch_all(
        """
        SELECT feature, guard_verdict, count(*) AS calls FROM ai_prompt_logs
        WHERE created_at > NOW() - make_interval(hours => $1) AND guard_verdict IS NOT NULL
        GROUP BY feature, guard_verdict ORDER BY feature, guard_verdict
        """,
        hours,
    )
    return {"hours": hours, "models": models, "steps": spans, "guard": guard}


async def list_traces(limit: int, kind: Optional[str], status: Optional[str]) -> List[Dict[str, Any]]:
    return await fetch_all(
        """
        SELECT trace_id, min(started_at) AS started_at, count(*) AS spans,
               max(started_at + make_interval(secs => duration_ms / 1000)) - min(started_at) AS elapsed,
               array_agg(DISTINCT kind) AS kinds,
               count(*) FILTER (WHERE status <> 'OK') AS not_ok,
               (array_agg(user_id) FILTER (WHERE user_id IS NOT NULL))[1] AS user_id,
               (array_agg(name ORDER BY started_at) FILTER (WHERE parent_id IS NULL))[1] AS root
        FROM ai_trace_spans
        WHERE started_at > NOW() - INTERVAL '7 days'
        GROUP BY trace_id
        HAVING ($2::text IS NULL OR bool_or(kind = $2))
           AND ($3::text IS NULL OR bool_or(status = $3))
        ORDER BY min(started_at) DESC
        LIMIT $1
        """,
        limit, kind, status,
    )


async def get_trace(trace_id: str) -> Dict[str, Any]:
    spans = await fetch_all(
        "SELECT * FROM ai_trace_spans WHERE trace_id = $1 ORDER BY started_at", trace_id,
    )
    prompts = await fetch_all(
        "SELECT * FROM ai_prompt_logs WHERE trace_id = $1 ORDER BY created_at", trace_id,
    )
    for s in spans:
        s["attributes"] = _attrs(s["attributes"])
    for p in prompts:
        p["pii_redactions"] = _attrs(p["pii_redactions"])
    return {"trace_id": trace_id, "spans": spans, "prompts": prompts}


async def list_prompt_logs(limit: int, offset: int, feature: Optional[str], status: Optional[str],
                           guard_verdict: Optional[str]) -> Dict[str, Any]:
    where = """
        WHERE ($1::text IS NULL OR feature = $1) AND ($2::text IS NULL OR status = $2)
          AND ($3::text IS NULL OR guard_verdict = $3)
    """
    rows = await fetch_all(
        f"SELECT * FROM ai_prompt_logs {where} ORDER BY created_at DESC LIMIT $4 OFFSET $5",
        feature, status, guard_verdict, limit, offset,
    )
    total = await fetch_one(f"SELECT count(*) AS n FROM ai_prompt_logs {where}", feature, status, guard_verdict)
    for r in rows:
        r["pii_redactions"] = _attrs(r["pii_redactions"])
    return {"items": rows, "total": total["n"] if total else 0, "limit": limit, "offset": offset}
