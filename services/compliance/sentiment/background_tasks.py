"""Own the lifecycle of sentiment background tasks independently of API requests."""
import asyncio
import logging

from services.compliance.sentiment.config import _running, _tasks

logger = logging.getLogger(__name__)


async def _run(analysis_id, run):
    try:
        await run(analysis_id)
    except Exception:
        logger.exception("Sentiment analysis %s couldn't be saved", analysis_id)
    finally:
        _running.discard(analysis_id)


def start_tasks(analysis_ids, run):
    for analysis_id in analysis_ids:
        if analysis_id in _running:
            continue
        _running.add(analysis_id)
        task = asyncio.create_task(_run(analysis_id, run), name=f"sentiment-{analysis_id}")
        _tasks.add(task)
        task.add_done_callback(_tasks.discard)
