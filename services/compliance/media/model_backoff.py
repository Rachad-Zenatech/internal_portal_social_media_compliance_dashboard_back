"""Bounded retries for transient media-model outages after the normal fallback pass."""

import asyncio
import logging
import random
import re
import time

from services.ai.core.gemini_service import is_rotatable_model_error


logger = logging.getLogger(__name__)
TRANSIENT_CODES = {500, 502, 503, 504}
# Gemini rejects manually supplied deadlines below ten seconds with INVALID_ARGUMENT.
MIN_TIMEOUT_SEC = 10.


def error_code(error):
    code = getattr(error, "code", None) or getattr(error, "status_code", None)
    if code is None:
        code = getattr(getattr(error, "response", None), "status_code", None)
    if code is not None:
        try:
            return int(code)
        except (ValueError, TypeError):
            pass
    return None


def transient_failure(error):
    code = error_code(error)
    if code is not None:
        return code in TRANSIENT_CODES
    return bool(re.search(r"\b(?:500|502|503|504)\b|high demand|temporarily overloaded", str(error), re.I))


def fallback_failure(error):
    code = error_code(error)
    if code in (401, 403):
        return False
    if code == 400 and "deadline" in str(error).lower():
        return False
    if code in (400, 404, 429):
        return True
    return (transient_failure(error) or is_rotatable_model_error(error)
            or isinstance(error, TimeoutError) or "Timeout" in type(error).__name__)


async def generate(models, request, *, timeout_sec, retry_rounds=2, retry_budget_sec=20):
    """Try all models immediately, then back off only for temporary server failures.

    request(model, timeout_sec, attempt) must enforce the supplied HTTP timeout. Keeping
    this timeout in the SDK avoids abandoning an active synchronous request in a thread.
    Quota errors, missing models and ordinary timeouts retain their single fallback pass.
    """
    pending = list(models)
    last_error = None
    deadline = None
    for round_number in range(max(0, min(3, retry_rounds)) + 1):
        if not pending:
            break
        if round_number:
            remaining = deadline - time.monotonic()
            delay = min(5., 2 ** (round_number - 1) + random.uniform(0., .25))
            if remaining - delay < MIN_TIMEOUT_SEC:
                break
            logger.info("Media models busy; retry round %s in %.2fs", round_number, delay)
            await asyncio.sleep(delay)
        retry = []
        for model in pending:
            timeout = max(MIN_TIMEOUT_SEC, timeout_sec)
            if round_number:
                remaining = deadline - time.monotonic()
                if remaining < MIN_TIMEOUT_SEC:
                    break
                timeout = min(timeout, remaining)
            try:
                return await request(model, timeout, round_number + 1)
            except Exception as error:
                if not fallback_failure(error):
                    raise
                logger.info("Media model %s unavailable on attempt %s (%s)",
                            model, round_number + 1, str(error)[:120])
                last_error = error
                if transient_failure(error):
                    retry.append(model)
        pending = retry
        if deadline is None:
            deadline = time.monotonic() + max(0., min(60., retry_budget_sec))
    raise RuntimeError(f"No image-text model is available right now: {last_error}") from last_error
