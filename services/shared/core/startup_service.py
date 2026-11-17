"""Server startup helpers for tracked background tasks."""

import asyncio
from functools import partial
from typing import Any, Coroutine, List

from services.shared.core.logging_service import log_background_task_result

def spawn_background(tasks: List[asyncio.Task], coro: Coroutine[Any, Any, Any], name: str) -> asyncio.Task:
    """Start a background task whose failure is logged, and keep it in `tasks` so shutdown can cancel it."""
    task = asyncio.create_task(coro, name=name)
    task.add_done_callback(partial(log_background_task_result, task_name=name))
    tasks.append(task)
    return task
