"""Configuration for the optional full rulebook model pass."""

import os


def _full_rule_mode() -> str:
    """off (default): the trigger phrases and the sentence-by-sentence search pick what the AI reads; auto: also read the post
    against every rule when it reads like it makes claims; always: every post. The extra pass costs one AI call per post
    and, on the test posts, added little over the phrases and the search."""
    raw = os.getenv("COMPLIANCE_FULL_RULE_PASS", "off").strip().lower()
    if raw in {"1", "true", "yes", "on", "always"}:
        return "always"
    if raw == "auto":
        return "auto"
    return "off"
