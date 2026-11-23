"""Configuration for the full rulebook model pass."""

import os


def _full_rule_mode() -> str:
    """always (default): review every post; auto: add a full pass for claims; off: use search-selected rules.
    Exact-content caching preserves repeatability; explicit overrides retain selective modes."""
    raw = os.getenv("COMPLIANCE_FULL_RULE_PASS", "always").strip().lower()
    if raw in {"1", "true", "yes", "on", "always"}:
        return "always"
    if raw == "auto":
        return "auto"
    return "off"
