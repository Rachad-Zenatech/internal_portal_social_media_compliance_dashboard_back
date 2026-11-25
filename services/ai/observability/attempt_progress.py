"""Attach task-local display details to model attempt events, including retries."""
from contextlib import contextmanager

from . import ai_observability


@contextmanager
def attempt_progress(**details):
    listener = ai_observability.attempt_listener.get()
    if listener is None:
        yield
        return
    token = ai_observability.attempt_listener.set(lambda event: listener({**event, **details}))
    try:
        yield
    finally:
        ai_observability.attempt_listener.reset(token)


@contextmanager
def capture_attempts():
    attempts = {}
    outer = ai_observability.attempt_listener.get()
    fields = ("id", "status", "model", "feature", "attempt", "tier", "error_kind", "error", "duration_ms", "progress_label")
    def record(event):
        if event.get("event") == "attempt" and event.get("id"):
            attempts[event["id"]] = {key: event.get(key) for key in fields}
        if outer is not None:
            outer(event)
    token = ai_observability.attempt_listener.set(record)
    try:
        yield attempts
    finally:
        ai_observability.attempt_listener.reset(token)
