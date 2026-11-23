"""Bound compliance attempts and share outage knowledge without changing judgment prompts or coverage."""
import os
from google.genai import types
from services.ai.core import gemini_service

FEATURES = frozenset({"COMPLIANCE_CHECK", "CONTEXT_EVAL", "SOURCE_CHECK", "VISUAL_COMPLIANCE"})


def configure(feature, config):
    if feature not in FEATURES:
        return config
    config = config or types.GenerateContentConfig()
    if not isinstance(config, types.GenerateContentConfig):
        return config
    seconds = float(os.getenv("COMPLIANCE_MODEL_TIMEOUT_SEC", "20"))
    if feature == "VISUAL_COMPLIANCE":
        seconds = float(os.getenv("VISUAL_COMPLIANCE_TIMEOUT_SEC", "30"))
    limit = max(1000, int(seconds * 1000))
    http = config.http_options or types.HttpOptions()
    timeout = min(http.timeout, limit) if http.timeout and http.timeout > 0 else limit
    retry = http.retry_options or types.HttpRetryOptions()
    # The app already rotates models. Hidden SDK retries otherwise multiply each visible attempt.
    http = http.model_copy(update={"timeout": timeout, "retry_options": retry.model_copy(update={"attempts": 1})})
    return config.model_copy(update={"http_options": http})


def text_models(preferred):
    """Text judgments may use every explicitly configured general fallback, including those used by rulebook checks."""
    return gemini_service.without_retired(list(dict.fromkeys([*preferred, *gemini_service.get_gemini_fallback_models()])))
