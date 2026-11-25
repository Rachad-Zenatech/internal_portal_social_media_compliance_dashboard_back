"""Background work registered during the API application's lifespan."""
from __future__ import annotations

import logging
from typing import Callable, Coroutine


async def _resume_interrupted_reads(logger: logging.Logger) -> None:
    from services.compliance.evaluation import brand_voice_service
    from services.compliance.media import document_text_service, image_text_service

    images = await image_text_service.resume_interrupted()
    documents = await document_text_service.resume_interrupted()
    if images or documents:
        logger.info("Resumed %d image and %d document read(s) interrupted by a restart", images, documents)
    manuals = await brand_voice_service.resume_interrupted()
    if manuals:
        logger.info("Resumed reading %d brand manual(s) interrupted by a restart", manuals)


def start_background_services(
    tasks: list,
    spawn: Callable[[list, Coroutine, str], None],
    logger: logging.Logger,
    graph_sync_enabled: bool,
) -> None:
    """Register non-critical startup work without coupling it to the HTTP module."""
    from mcp_tools import sync_mcp_tools_to_vector_db
    from services.ai.observability import ai_observability
    from services.compliance.analytics.listening_providers import warm_up_sentiment_model
    from services.compliance.evaluation.newsroom_sync_service import newsroom_sync_loop
    from services.compliance.evaluation.policy_watch_service import policy_watch_loop
    from services.compliance.evaluation.reference_index_service import sync_soon
    from services.compliance.evaluation.script_doc_realtime import start_listener as start_script_doc_relay
    from services.compliance.evaluation.script_review_service import resume_interrupted_sends
    from services.compliance.submissions.finding_precedent_service import remember_all_in_background as remember_verdicts
    from services.compliance.submissions.review_deadline_service import deadline_loop
    from services.compliance.submissions.review_handoff_release import handoff_loop
    from services.compliance.submissions.review_memory_service import remember_all_in_background as remember_reviews
    from services.notifications.review_email.scheduler import email_loop
    from services.microsoft_graph.graph_sync_job import graph_sync_loop
    from services.shared.core.cache_service import start_cache_invalidation_listener

    spawn(tasks, start_cache_invalidation_listener(), "cache-listener")
    spawn(tasks, start_script_doc_relay(), "script-doc-relay")
    spawn(tasks, sync_mcp_tools_to_vector_db(), "mcp-tool-sync")
    if graph_sync_enabled:
        spawn(tasks, graph_sync_loop(), "graph-user-sync")
    else:
        logger.info("Graph background sync disabled (set GRAPH_BACKGROUND_SYNC_ENABLED=true to enable)", extra={"event": "graph_sync_disabled"})
    spawn(tasks, deadline_loop(), "review-deadlines")
    spawn(tasks, handoff_loop(), "review-handoffs")
    spawn(tasks, email_loop(), "review-emails")
    spawn(tasks, remember_reviews(), "review-memory-sync")
    spawn(tasks, remember_verdicts(), "finding-precedent-sync")
    spawn(tasks, newsroom_sync_loop(), "newsroom-sync")
    spawn(tasks, policy_watch_loop(), "policy-watch")
    spawn(tasks, _resume_interrupted_reads(logger), "resume-interrupted-reads")
    spawn(tasks, resume_interrupted_sends(), "script-send-resume")
    spawn(tasks, warm_up_sentiment_model(), "sentiment-model-warm-up")
    spawn(tasks, ai_observability.writer_loop(), "ai-trace-writer")
    sync_soon()
