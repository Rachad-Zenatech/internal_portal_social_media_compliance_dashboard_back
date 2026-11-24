"""Combined AI health, feature-to-core mapping, and usage overview for System & Security."""

from datetime import datetime, timezone
import os
from typing import Any, Dict, List, Optional
from uuid import UUID

from postgresql_db.database import fetch_all
from services.ai.observability import ai_health, ai_usage_service

# Every user-facing or background AI feature paired with the core engine(s) it runs on and why.
_FEATURE_CATALOG: List[Dict[str, Any]] = [
    {"feature": "COMPLIANCE_CHECK", "purpose": "Screens captions, scripts, and transcripts against active rulebook rules and messaging precedent.", "primary_core": "bedrock_llm", "cores": ["bedrock_llm", "titan_embeddings"], "routed": True},
    {"feature": "VISUAL_COMPLIANCE", "purpose": "Evaluates attached images and sampled video frames against visual compliance rules.", "primary_core": "bedrock_llm", "cores": ["bedrock_llm"], "routed": False},
    {"feature": "SENTIMENT", "purpose": "Produces editorial tone, brand-fit, and controversy findings with precedent retrieval.", "primary_core": "bedrock_llm", "cores": ["bedrock_llm", "titan_embeddings"], "routed": False},
    {"feature": "SOURCE_CHECK", "purpose": "Verifies factual claims in posts against cited URLs and web search results.", "primary_core": "bedrock_llm", "cores": ["bedrock_llm"], "routed": False},
    {"feature": "IMAGE_TEXT", "purpose": "Extracts text inside uploaded graphics and detects faces anonymously before review.", "primary_core": "bedrock_llm", "cores": ["bedrock_llm", "local_yunet_faces"], "routed": False},
    {"feature": "IMAGE_SOURCE_OCR", "purpose": "Reads URLs, titles, and source attributions printed inside post images.", "primary_core": "bedrock_llm", "cores": ["bedrock_llm"], "routed": False},
    {"feature": "IMAGE_FINDING_REGIONS", "purpose": "Pinpoints bounding boxes in images for flagged compliance wording.", "primary_core": "bedrock_llm", "cores": ["bedrock_llm"], "routed": False},
    {"feature": "TRANSCRIPTION", "purpose": "Transcribes spoken video audio locally on CPU and reads on-screen text overlays.", "primary_core": "local_whisper", "cores": ["local_whisper", "bedrock_llm"], "routed": True},
    {"feature": "CAPTION", "purpose": "Drafts rulebook-checked caption suggestions tailored to selected platforms.", "primary_core": "bedrock_llm", "cores": ["bedrock_llm"], "routed": True},
    {"feature": "SCRIPT_FIX", "purpose": "Generates targeted wording rewrites for flagged script lines.", "primary_core": "bedrock_llm", "cores": ["bedrock_llm"], "routed": False},
    {"feature": "BRAND_VOICE", "purpose": "Extracts voice, tone, color palette, and logo candidates from uploaded brand PDFs.", "primary_core": "bedrock_llm", "cores": ["bedrock_llm"], "routed": False},
    {"feature": "CHAT", "purpose": "Answers rulebook, Messaging Context, and portal questions via RAG + semantic cache.", "primary_core": "bedrock_llm", "cores": ["bedrock_llm", "titan_embeddings"], "routed": True},
    {"feature": "PUBLIC_MOOD", "purpose": "Summarizes scraped social/news sentiment and recommends POST, WAIT, or HOLD.", "primary_core": "bedrock_llm", "cores": ["bedrock_llm", "local_nlp_sentiment", "xgboost_mood", "public_mood_scraper"], "routed": True},
    {"feature": "PUBLIC_MOOD_QUERY", "purpose": "Plans targeted social and news search queries from scheduled post content.", "primary_core": "bedrock_llm", "cores": ["bedrock_llm"], "routed": False},
    {"feature": "EMBEDDINGS", "purpose": "Generates vector embeddings for Messaging Context, review memory, and semantic cache.", "primary_core": "titan_embeddings", "cores": ["titan_embeddings"], "routed": False},
    {"feature": "PUBLIC_MOOD_SCRAPE", "purpose": "Collects live news & social posts for Financial ZenaTech and Today's scheduled posts.", "primary_core": "public_mood_scraper", "cores": ["public_mood_scraper", "local_nlp_sentiment"], "routed": False},
]

_CORE_LABELS: Dict[str, str] = {
    "bedrock_llm": "AWS Bedrock (Claude LLM)",
    "titan_embeddings": "Amazon Titan + Atlas Vector Store",
    "local_nlp_sentiment": "Local Sentiment NLP (FinBERT / RoBERTa / VADER)",
    "local_whisper": "Local Speech-to-Text (Whisper CPU)",
    "local_yunet_faces": "Local Face Detection (OpenCV YuNet)",
    "xgboost_mood": "XGBoost Mood Classifier",
    "public_mood_scraper": "Public Mood Live Scraper",
}


def _routing_tier(feature: str, routed: bool) -> str:
    if not routed:
        return "DIRECT"
    try:
        from services.ai.core import model_router

        return model_router.base_tier(feature)
    except Exception:
        return "STANDARD"


def _features_by_core() -> Dict[str, List[str]]:
    grouped: Dict[str, List[str]] = {c: [] for c in _CORE_LABELS}
    for entry in _FEATURE_CATALOG:
        for cid in entry["cores"]:
            grouped.setdefault(cid, []).append(entry["feature"])
    return grouped


def _probe_cloud_cores(
    health: ai_health.AiStatus,
    feature_status: Dict[str, str],
    model_rows: List[Dict[str, Any]],
    step_rows: List[Dict[str, Any]],
    by_core: Dict[str, List[str]],
) -> List[Dict[str, Any]]:
    from services.ai.core import bedrock_embeddings, bedrock_service, model_router, vector_store_service

    bedrock_enabled = bedrock_service.is_bedrock_enabled()
    bedrock_profile = bedrock_service.get_bedrock_profile_id() or "Not set"
    bedrock_region = bedrock_service.get_bedrock_region() or "Not set"
    has_key = bedrock_service.has_ai_key()

    llm_calls = sum(int(r.get("calls") or 0) for r in model_rows)
    llm_errors = sum(int(r.get("errors") or 0) for r in model_rows)
    llm_tokens = sum(int(r.get("input_tokens") or 0) + int(r.get("output_tokens") or 0) for r in model_rows)
    embed_calls = sum(int(r.get("calls") or 0) for r in step_rows if r.get("kind") in {"embedding", "retrieval"})

    bedrock_status = "OUT"
    if has_key:
        if not health.available:
            bedrock_status = "OVERFLOW" if health.reason in {"QUOTA", "BUSY"} else "OUT"
        elif any(feature_status.get(f["feature"]) == "OUT" for f in _FEATURE_CATALOG if f["primary_core"] == "bedrock_llm"):
            bedrock_status = "OUT"
        elif any(feature_status.get(f["feature"]) == "OVERFLOW" for f in _FEATURE_CATALOG if f["primary_core"] == "bedrock_llm"):
            bedrock_status = "OVERFLOW"
        else:
            bedrock_status = "OK" if llm_calls > 0 else "READY"

    embed_pause = vector_store_service.embedding_pause_status()
    mongo_enabled = vector_store_service.is_enabled()
    embed_status, embed_note = "UNCONFIGURED", "MONGODB_URI not set (vector store disabled)"
    if mongo_enabled and bedrock_enabled:
        if embed_pause:
            reason_msg, left_sec = embed_pause
            embed_status = "OUT" if "denied" in reason_msg.lower() else "OVERFLOW"
            embed_note = f"{reason_msg} (~{max(1, left_sec // 60)}m pause; keyword fallback active)"
        else:
            embed_status = "OK" if embed_calls > 0 else "READY"
            embed_note = f"Vector search active ({bedrock_embeddings.dimensions()}d)"

    return [
        {
            "core": "bedrock_llm",
            "name": _CORE_LABELS["bedrock_llm"],
            "provider": "AWS Bedrock",
            "status": bedrock_status,
            "configured": bedrock_enabled,
            "model": bedrock_profile,
            "detail": f"Region {bedrock_region} · Tiers: LIGHT/STANDARD/HEAVY ({len(model_router.tier_models('STANDARD'))} models)",
            "note": health.title if not health.available else (f"{llm_calls} calls, {llm_errors} errors in window" if llm_calls else "Ready (no LLM calls in window)"),
            "calls": llm_calls,
            "tokens": llm_tokens,
            "features": by_core["bedrock_llm"],
        },
        {
            "core": "titan_embeddings",
            "name": _CORE_LABELS["titan_embeddings"],
            "provider": "AWS Bedrock + MongoDB Atlas",
            "status": embed_status,
            "configured": mongo_enabled and bedrock_enabled,
            "model": f"{bedrock_embeddings.model_id()} ({bedrock_embeddings.dimensions()}d)",
            "detail": "Semantic RAG for rulebook, review precedent & semantic cache",
            "note": embed_note,
            "calls": embed_calls,
            "tokens": 0,
            "features": by_core["titan_embeddings"],
        },
    ]


def _probe_local_cores(health: ai_health.AiStatus, by_core: Dict[str, List[str]]) -> List[Dict[str, Any]]:
    try:
        from services.compliance.analytics import item_sentiment

        fin_state = getattr(item_sentiment, "_financial_model", None)
        nlp_status = "FALLBACK" if fin_state is False else "OK"
        nlp_model = f"{item_sentiment.FINANCIAL_MODEL_NAME} + {item_sentiment.LOCAL_MODEL_NAME} (VADER fallback)"
        nlp_note = "FinBERT unavailable; using VADER fallback" if fin_state is False else ("Loaded in memory" if fin_state else "Loads on demand (VADER fallback ready)")
    except Exception:
        nlp_status, nlp_model, nlp_note = "OK", "FinBERT / RoBERTa / VADER", "On-demand CPU NLP"

    try:
        from services.compliance.media import local_speech

        whisper_note = "Whisper CPU pipeline loaded" if getattr(local_speech, "_pipeline", None) is not None else "On-demand (loads when transcribing video)"
    except Exception:
        whisper_note = "On-demand CPU speech transcription"

    try:
        from services.compliance.media import face_detection_service

        yunet_ok = face_detection_service._MODEL_PATH.is_file()
    except Exception:
        yunet_ok = False

    try:
        from services.compliance.analytics import mood_model

        xgb_installed, xgb_mode = mood_model.is_xgboost_available(), mood_model.mode()
    except Exception:
        xgb_installed, xgb_mode = False, "off"

    scrape_issue = next((i for i in health.degraded_features if i.feature == "PUBLIC_MOOD_SCRAPE"), None)
    try:
        from tools.compliance.analytics import listening_router

        scrape_state = str((listening_router._scrape or {}).get("state") or "idle")
    except Exception:
        scrape_state = "idle"

    return [
        {
            "core": "local_nlp_sentiment",
            "name": _CORE_LABELS["local_nlp_sentiment"],
            "provider": "In-Process CPU (HuggingFace + VADER)",
            "status": nlp_status,
            "configured": True,
            "model": nlp_model,
            "detail": "Scores scraped news & X posts item-by-item before LLM summary",
            "note": nlp_note,
            "calls": 0,
            "tokens": 0,
            "features": by_core["local_nlp_sentiment"],
        },
        {
            "core": "local_whisper",
            "name": _CORE_LABELS["local_whisper"],
            "provider": "In-Process CPU (FFmpeg + Whisper)",
            "status": "OK",
            "configured": True,
            "model": "Whisper CPU (local audio)",
            "detail": "Keeps private video speech on-host before compliance review",
            "note": whisper_note,
            "calls": 0,
            "tokens": 0,
            "features": by_core["local_whisper"],
        },
        {
            "core": "local_yunet_faces",
            "name": _CORE_LABELS["local_yunet_faces"],
            "provider": "OpenCV ONNX",
            "status": "OK" if yunet_ok else "OUT",
            "configured": yunet_ok,
            "model": "face_detection_yunet_2023mar.onnx",
            "detail": "Detects anonymous face regions locally before image OCR",
            "note": "Model file ready (falls back to Bedrock Vision if needed)" if yunet_ok else "ONNX model missing",
            "calls": 0,
            "tokens": 0,
            "features": by_core["local_yunet_faces"],
        },
        {
            "core": "xgboost_mood",
            "name": _CORE_LABELS["xgboost_mood"],
            "provider": "Local XGBoost",
            "status": "READY" if xgb_installed and xgb_mode != "off" else "UNCONFIGURED",
            "configured": xgb_installed,
            "model": f"Mode: {xgb_mode}",
            "detail": "Predicts POST / WAIT / HOLD alongside deterministic public-mood rules",
            "note": f"XGBoost {'installed' if xgb_installed else 'not installed'} · mode={xgb_mode}",
            "calls": 0,
            "tokens": 0,
            "features": by_core["xgboost_mood"],
        },
        {
            "core": "public_mood_scraper",
            "name": _CORE_LABELS["public_mood_scraper"],
            "provider": "Playwright + RSS + Stocktwits",
            "status": scrape_issue.status if scrape_issue else "OK",
            "configured": True,
            "model": "Headless Chromium + RSS feeds",
            "detail": "Collects Financial ZenaTech & Today's Posts public reaction",
            "note": scrape_issue.message if scrape_issue else f"Scraper state: {scrape_state}",
            "calls": 0,
            "tokens": 0,
            "features": by_core["public_mood_scraper"],
        },
    ]


def _build_features(
    health: ai_health.AiStatus,
    feature_rows: List[Dict[str, Any]],
    usage_24h: Dict[str, Any],
) -> tuple[List[Dict[str, Any]], Dict[str, str]]:
    stats_by_feature = {str(r["feature"]).upper(): dict(r) for r in feature_rows}
    issues_by_feature = {i.feature: i for i in health.degraded_features}
    catalog_map = {c["feature"]: c for c in _FEATURE_CATALOG}
    extra = [f for f in stats_by_feature if f not in catalog_map] + [
        f for f in issues_by_feature if f not in catalog_map and f not in stats_by_feature
    ]
    all_specs = list(_FEATURE_CATALOG) + [
        {"feature": f, "purpose": "Runtime traced AI capability.", "primary_core": "bedrock_llm", "cores": ["bedrock_llm"], "routed": False}
        for f in sorted(set(extra))
    ]

    features_out: List[Dict[str, Any]] = []
    status_map: Dict[str, str] = {}
    for spec in all_specs:
        feat = spec["feature"]
        st = stats_by_feature.get(feat, {})
        issue = issues_by_feature.get(feat)
        calls = int(st.get("calls") or 0)
        if issue is not None:
            feat_status, issue_reason, issue_message, issue_since = issue.status, issue.reason, issue.message, issue.since
        elif not health.available and "bedrock_llm" in spec["cores"]:
            feat_status = "OVERFLOW" if health.reason in {"QUOTA", "BUSY"} else "OUT"
            issue_reason, issue_message, issue_since = health.reason, health.title, health.since
        elif calls > 0:
            feat_status, issue_reason, issue_message, issue_since = "OK", None, None, None
        else:
            feat_status, issue_reason, issue_message, issue_since = "READY", None, None, None

        status_map[feat] = feat_status
        features_out.append({
            "feature": feat,
            "label": ai_health._feature_label(feat),
            "purpose": spec["purpose"],
            "primary_core": spec["primary_core"],
            "primary_core_label": _CORE_LABELS.get(spec["primary_core"], spec["primary_core"]),
            "cores": [{"id": cid, "label": _CORE_LABELS.get(cid, cid)} for cid in spec["cores"]],
            "routing_tier": _routing_tier(feat, spec["routed"]),
            "status": feat_status,
            "healthy": feat_status in {"OK", "READY"},
            "issue_reason": issue_reason,
            "issue_message": issue_message,
            "issue_since": issue_since.isoformat() if issue_since else None,
            "calls": calls,
            "errors": int(st.get("errors") or 0),
            "blocked": int(st.get("blocked") or 0),
            "cache_hits": int(st.get("cache_hits") or 0),
            "input_tokens": int(st.get("input_tokens") or 0),
            "output_tokens": int(st.get("output_tokens") or 0),
            "avg_latency_ms": float(st["avg_latency_ms"]) if st.get("avg_latency_ms") is not None else None,
            "p95_latency_ms": float(st["p95_latency_ms"]) if st.get("p95_latency_ms") is not None else None,
            "models_used": list(st.get("models_used") or []),
            "last_call_at": st["last_call_at"].isoformat() if st.get("last_call_at") else None,
            "last_status": st.get("last_status"),
            "last_error_type": st.get("last_error_type"),
            "usage_24h": usage_24h.get(feat),
        })
    return features_out, status_map


async def _query_telemetry(hours: int) -> tuple[List[Dict[str, Any]], List[Dict[str, Any]], List[Dict[str, Any]], List[Dict[str, Any]]]:
    feature_rows = await fetch_all(
        """
        SELECT feature, count(*) AS calls,
               count(*) FILTER (WHERE status = 'ERROR') AS errors,
               count(*) FILTER (WHERE status = 'BLOCKED') AS blocked,
               count(*) FILTER (WHERE cache_hit) AS cache_hits,
               coalesce(sum(input_tokens), 0) AS input_tokens,
               coalesce(sum(output_tokens), 0) AS output_tokens,
               round(avg(latency_ms)::numeric, 1) AS avg_latency_ms,
               round((percentile_cont(0.95) WITHIN GROUP (ORDER BY latency_ms))::numeric, 1) AS p95_latency_ms,
               array_remove(array_agg(DISTINCT model), NULL) AS models_used,
               max(created_at) AS last_call_at,
               (array_agg(status ORDER BY created_at DESC))[1] AS last_status,
               (array_agg(error_type ORDER BY created_at DESC) FILTER (WHERE status <> 'OK'))[1] AS last_error_type
        FROM ai_prompt_logs WHERE created_at > NOW() - make_interval(hours => $1) GROUP BY feature
        """,
        hours,
    )
    model_rows = await fetch_all(
        """
        SELECT feature, model, count(*) AS calls,
               count(*) FILTER (WHERE status = 'ERROR') AS errors,
               count(*) FILTER (WHERE status = 'BLOCKED') AS blocked,
               count(*) FILTER (WHERE cache_hit) AS cache_hits,
               coalesce(sum(input_tokens), 0) AS input_tokens,
               coalesce(sum(output_tokens), 0) AS output_tokens,
               round(avg(latency_ms)::numeric, 1) AS avg_latency_ms,
               round((percentile_cont(0.95) WITHIN GROUP (ORDER BY latency_ms))::numeric, 1) AS p95_latency_ms
        FROM ai_prompt_logs WHERE created_at > NOW() - make_interval(hours => $1)
        GROUP BY feature, model ORDER BY calls DESC, feature
        """,
        hours,
    )
    step_rows = await fetch_all(
        """
        SELECT kind, name, count(*) AS calls,
               count(*) FILTER (WHERE status <> 'OK') AS not_ok,
               round(avg(duration_ms)::numeric, 1) AS avg_ms,
               round((percentile_cont(0.95) WITHIN GROUP (ORDER BY duration_ms))::numeric, 1) AS p95_ms
        FROM ai_trace_spans
        WHERE started_at > NOW() - make_interval(hours => $1) AND kind IN ('tool', 'retrieval', 'embedding', 'cache')
        GROUP BY kind, name ORDER BY kind, calls DESC
        """,
        hours,
    )
    guard_rows = await fetch_all(
        """
        SELECT feature, guard_verdict, count(*) AS calls
        FROM ai_prompt_logs
        WHERE created_at > NOW() - make_interval(hours => $1) AND guard_verdict IS NOT NULL
        GROUP BY feature, guard_verdict ORDER BY feature, guard_verdict
        """,
        hours,
    )
    return ([dict(r) for r in feature_rows], [dict(r) for r in model_rows], [dict(r) for r in step_rows], [dict(r) for r in guard_rows])


async def overview(hours: int = 24, user_id: Optional[UUID] = None) -> Dict[str, Any]:
    """Return full AI health (what is out/overflow/working), feature-to-core mappings, and usage telemetry."""
    health = await ai_health.status()
    feature_rows, model_rows, step_rows, guard_rows = await _query_telemetry(hours)

    usage_24h: Dict[str, Any] = {}
    if user_id:
        try:
            usage_24h = await ai_usage_service.usage_for(user_id)
        except Exception:
            usage_24h = {}

    features_out, status_map = _build_features(health, feature_rows, usage_24h)
    by_core = _features_by_core()
    cores_out = _probe_cloud_cores(health, status_map, model_rows, step_rows, by_core) + _probe_local_cores(health, by_core)

    totals = {
        "calls": sum(f["calls"] for f in features_out),
        "errors": sum(f["errors"] for f in features_out),
        "blocked": sum(f["blocked"] for f in features_out),
        "cache_hits": sum(f["cache_hits"] for f in features_out),
        "input_tokens": sum(f["input_tokens"] for f in features_out),
        "output_tokens": sum(f["output_tokens"] for f in features_out),
        "features_total": len(features_out),
        "features_out": sum(1 for f in features_out if f["status"] == "OUT"),
        "features_overflow": sum(1 for f in features_out if f["status"] == "OVERFLOW"),
        "features_healthy": sum(1 for f in features_out if f["healthy"]),
    }

    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "hours": hours,
        "health": health.model_dump(mode="json"),
        "totals": totals,
        "cores": cores_out,
        "features": features_out,
        "models": model_rows,
        "steps": step_rows,
        "guard": guard_rows,
    }
