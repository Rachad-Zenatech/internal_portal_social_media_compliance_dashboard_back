"""Run native PDF operations in disposable processes, never in API worker threads.

The pickle channel is private to our parent/child processes. Uploaded PDF bytes
are arguments inside that channel, never deserialized as a pickle themselves.
"""
import contextlib
import importlib
import logging
import os
import pickle
import subprocess
import sys
import threading
from functools import wraps

logger = logging.getLogger(__name__)
_slots = threading.BoundedSemaphore(max(1, min(4, int(os.getenv("PDF_PROCESS_WORKERS", "2")))))
_OPERATIONS = {
    "layout": ("services.compliance.media.document_layout", "pdf_to_text"),
    "brand_facts": ("services.compliance.evaluation.brand_voice_service", "read_pdf"),
    "brand_logo": ("services.compliance.evaluation.brand_voice_service", "crop_logo"),
    "manual_sections": ("services.compliance.evaluation.brand_manual_sections", "read_sections"),
    "manual_page": ("services.compliance.evaluation.brand_manual_sections", "render_page"),
}


class PdfProcessingError(RuntimeError):
    """A PDF worker crashed, timed out, or could not read the document."""


def run(operation, args, kwargs):
    if operation not in _OPERATIONS:
        raise ValueError("Unknown PDF operation")
    if not _slots.acquire(timeout=float(os.getenv("PDF_PROCESS_QUEUE_TIMEOUT_SEC", "10"))):
        raise PdfProcessingError("PDF readers are busy; retry this document shortly.")
    try:
        result = subprocess.run(
            [sys.executable, "-m", __name__, operation],
            input=pickle.dumps((args, kwargs)), stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            timeout=float(os.getenv("PDF_PROCESS_TIMEOUT_SEC", "60")),
            env={**os.environ, "PYTHONFAULTHANDLER": "1"},
        )
        if result.returncode:
            logger.warning("PDF worker %s exited with code %s: %s", operation, result.returncode,
                           result.stderr.decode("utf-8", errors="replace")[-4000:])
            raise PdfProcessingError("The PDF reader stopped unexpectedly; the API remains available.")
        response = pickle.loads(result.stdout)
        if response[0] == "error":
            if response[3] is not None:
                from fastapi import HTTPException
                raise HTTPException(status_code=response[3], detail=response[2])
            raise PdfProcessingError(f"PDF processing failed ({response[1]}): {response[2]}")
        return response[1]
    except subprocess.TimeoutExpired as error:
        raise PdfProcessingError("The PDF reader timed out; upload a smaller or unlocked copy.") from error
    finally:
        _slots.release()


def isolated(operation):
    def decorate(function):
        @wraps(function)
        def wrapper(*args, **kwargs):
            return run(operation, args, kwargs)
        return wrapper
    return decorate


def _main():
    try:
        import resource
        resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
    except ImportError:  # Windows has no POSIX core dump setting.
        pass
    module, name = _OPERATIONS[sys.argv[1]]
    args, kwargs = pickle.loads(sys.stdin.buffer.read())
    with contextlib.redirect_stdout(sys.stderr):
        try:
            function = getattr(importlib.import_module(module), name).__wrapped__
            response = ("ok", function(*args, **kwargs))
        except Exception as error:
            response = ("error", type(error).__name__, getattr(error, "detail", str(error)),
                        getattr(error, "status_code", None))
    sys.stdout.buffer.write(pickle.dumps(response))


if __name__ == "__main__":
    _main()
