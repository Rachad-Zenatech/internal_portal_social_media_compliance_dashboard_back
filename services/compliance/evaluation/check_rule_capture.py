"""Capture the actual rule definitions used by a check, isolated per async task."""

from contextlib import contextmanager
from contextvars import ContextVar


_rules: ContextVar[dict | None] = ContextVar("compliance_snapshot_rules", default=None)


@contextmanager
def collecting():
    captured = {"rules": {}}
    token = _rules.set(captured)
    try:
        yield captured
    finally:
        _rules.reset(token)


def remember(rules, request) -> None:
    captured = _rules.get()
    if captured is not None:
        captured["request"] = request.model_dump(mode="json")
        for rule in rules:
            captured["rules"][str(rule.id)] = rule.model_dump(mode="json")
