"""Server startup helpers for schema updates and tracked background tasks."""

import asyncio
import importlib
import logging
from functools import partial
from typing import Any, Coroutine, List

from services.shared.core.logging_service import log_background_task_result

logger = logging.getLogger(__name__)

# The schema setups run at startup (in this order), and what a failure of each is called in the log.
SCHEMA_SETUPS = [
    ("setup_pbac_schema", "Schema verification note"),
    ("setup_social_media_schema", "Social media schema setup note"),
    ("setup_compliance_schema", "Compliance schema setup note"),
    ("setup_submissions_schema", "Submission schema setup note"),
    ("setup_context_schema", "Messaging context schema setup note"),
    ("setup_listening_schema", "Listening schema setup note"),
    ("setup_login_codes_schema", "Login codes schema setup note"),
    ("setup_performance_schema", "Post performance schema setup note"),
    ("setup_ai_observability_schema", "AI observability schema setup note"),
    ("setup_script_batches_schema", "Script batch schema setup note"),
    ("setup_script_docs_schema", "Script document schema setup note"),
    ("setup_teams_schema", "Teams schema setup note"),
    ("setup_source_trust_schema", "Source trust schema setup note"),
]


async def run_schema_setups(pool) -> None:
    """Apply schema changes once at a time across workers; fail startup if any setup fails."""
    async with pool.acquire() as conn:
        # A session lock spans setups that acquire and commit their own connections.
        # Poll rather than waiting inside PostgreSQL's short statement/lock timeouts.
        while not await conn.fetchval("SELECT pg_try_advisory_lock(8009, 1)"):
            await asyncio.sleep(0.5)
        try:
            for module_name, note in SCHEMA_SETUPS:
                try:
                    module = importlib.import_module(f"postgresql_db.schemas.{module_name}")
                    await module.setup()
                except Exception:
                    logger.exception("%s failed; stopping startup", note)
                    raise
        finally:
            await conn.execute("SELECT pg_advisory_unlock(8009, 1)")


async def resume_reads() -> None:
    """Resume image, document and brand-manual reads interrupted by a restart."""
    from services.compliance.media import document_text_service, image_text_service
    from services.compliance.evaluation import brand_voice_service

    images = await image_text_service.resume_interrupted()
    documents = await document_text_service.resume_interrupted()
    if images or documents:
        logger.info("Resumed %d image and %d document read(s) interrupted by a restart", images, documents)
    manuals = await brand_voice_service.resume_interrupted()
    if manuals:
        logger.info("Resumed reading %d brand manual(s) interrupted by a restart", manuals)


def spawn_background(tasks: List[asyncio.Task], coro: Coroutine[Any, Any, Any], name: str) -> asyncio.Task:
    """Start a background task whose failure is logged, and keep it in `tasks` so shutdown can cancel it."""
    task = asyncio.create_task(coro, name=name)
    task.add_done_callback(partial(log_background_task_result, task_name=name))
    tasks.append(task)
    return task
