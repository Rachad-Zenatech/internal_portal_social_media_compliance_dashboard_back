import os
import logging
import time
from typing import Any, Optional, List
from google import genai
from google.genai import errors

logger = logging.getLogger(__name__)

def _normalize_ai_model_name(name: str) -> str:
    """
    Normalizes a model name from the environment variable list
    into the format expected by the Google GenAI SDK.
    E.g. "Gemini 2.5 Flash" -> "gemini-2.5-flash"
    """
    return name.strip().lower().replace(" ", "-")
DEPRECATED_MODELS = {
    "gemini-3-flash-preview",
    "gemini-2.0-flash",
    "gemini-2.0-pro",
    "gemini-2.0-flash-lite",
    "gemini-2-flash",
    "gemini-2-flash-lite",
    "gemini-2",
    "gemini-1.5-flash",
    "gemini-1.5-pro",
    "gemini-1.0-pro",
    "gemini-2.5-pro",
    # Google answers these with 404 ("no longer available to new users"); trying them only wastes a call and a wait.
    "gemini-2.5-flash",
    "gemini-2.5-flash-lite",
}
_PERMANENTLY_REMOVED_MODELS = {"gemini-3-flash-preview"}

_model_cooldowns: dict[str, float] = {}


def _model_failure_cooldown_seconds(error: Exception) -> int:
    """Avoid retrying a model known to be unavailable while a request burst is in progress."""
    status_code = str(getattr(error, "code", "") or getattr(error, "status_code", ""))
    message = f"{status_code} {error}".lower()
    if "404" in message or "not found" in message or "not available" in message:
        return 300
    if "429" in message or "503" in message or "resource_exhausted" in message or "quota" in message:
        return 60
    return 0


def model_is_available(model_name: str) -> bool:
    """Return false while a short provider-failure cooldown for this model is active."""
    until = _model_cooldowns.get(model_name)
    if until is None:
        return True
    if until > time.monotonic():
        return False
    _model_cooldowns.pop(model_name, None)
    return True


def record_model_failure(model_name: str, error: Exception) -> None:
    cooldown = _model_failure_cooldown_seconds(error)
    if cooldown:
        _model_cooldowns[model_name] = time.monotonic() + cooldown


def without_retired(models):
    """The models that can still be called, in the same order. Falls back to the list as given if every one is retired,
    so a misconfigured list fails with the API's own error rather than with nothing to try."""
    kept = [m for m in models if m.strip().lower() not in DEPRECATED_MODELS]
    if kept:
        return kept
    # Keep the legacy "let the provider explain a wholly stale list" behavior, except for models we know must
    # never be called again. That prevents an environment override from reintroducing a guaranteed 404.
    return [m for m in models if m.strip().lower() not in _PERMANENTLY_REMOVED_MODELS]


def get_gemini_fallback_models() -> List[str]:
    """
    Retrieves and normalizes the list of available Gemini models
    from the environment configuration, filtering out deprecated/retired models.
    Guarantees active, proven general-purpose models are prioritized at the front of the rotation chain.
    """
    models_str = os.getenv("GEMINI_AVAILABLE_MODELS", "gemini-3.1-flash-lite,gemini-3.1-pro-preview")
    raw_models = [_normalize_ai_model_name(m) for m in models_str.split(",") if m.strip()]
    models = [m for m in raw_models if m not in DEPRECATED_MODELS]
    priority_order = ["gemini-3.1-flash-lite", "gemini-3.1-pro-preview"]
    ordered = [m for m in priority_order if m in models]
    for m in models:
        if m not in ordered:
            ordered.append(m)
    return ordered or ["gemini-3.1-flash-lite"]

def is_rotatable_model_error(e: Exception) -> bool:
    """
    Determines if a given exception represents a rate limit, quota exhaustion,
    temporary service spike, or deprecated/unavailable model (404 / 503 / 429).
    """
    try:
        from botocore.exceptions import ClientError
        if isinstance(e, ClientError):
            code = e.response.get("Error", {}).get("Code", "")
            if code in ("ThrottlingException", "ModelNotReadyException", "TooManyRequestsException", "ServiceUnavailableException"):
                return True
    except ImportError:
        pass

    if isinstance(e, errors.APIError):
        code = getattr(e, "code", getattr(e, "status", None))
        if code in (400, 404, 429, 503, 504):
            return True

        msg = str(e).lower()
        if any(k in msg for k in [
            "404", "not found", "no longer available", "not available",
            "429", "503", "resource_exhausted", "quota exceeded",
            "too many requests", "unavailable", "high demand", "deadline_exceeded", "deadline expired", "timed out",
            "is not supported", "invalid model", "deprecated"
        ]):
            return True

    # A model that stopped answering (the request's timeout ran out): try the next one.
    # (google-genai raises its own httpx2 timeouts, so match by name).
    if isinstance(e, TimeoutError) or "Timeout" in type(e).__name__:
        return True

    msg = str(e).lower()
    if any(k in msg for k in [
        "404", "not found", "no longer available", "not available",
        "429", "503", "resource_exhausted", "quota exceeded",
        "too many requests", "unavailable", "high demand", "timed out",
        "is not supported", "invalid model", "deprecated"
    ]):
        return True

    return False

# Backward compatibility alias
is_quota_error = is_rotatable_model_error

async def generate_content_with_fallback(
    client: genai.Client,
    contents: Any,
    config: Optional[Any] = None,
    feature: str = "GENERAL",
    submission_id: Optional[Any] = None,
) -> Any:
    """
    A robust wrapper around client.models.generate_content that automatically
    rotates through configured Gemini models upon encountering quota limits (429),
    server spikes (503), or deprecated/unavailable models (404).

    The model order comes from model_router (by feature and request size), and every
    attempt is traced and prompt-logged (ai_observability).
    """
    from services.ai.observability import ai_observability
    from services.ai.core import model_router
    from services.ai.core.bedrock_service import get_bedrock_client

    # Replace with Bedrock client (Gemini client commented out for now):
    # if is_bedrock_enabled() and (client is None or "Bedrock" not in type(client).__name__):
    #     client = get_bedrock_client()
    client = get_bedrock_client()

    prompt, attachments = ai_observability.describe_contents(contents)
    route = model_router.route(feature, prompt_chars=len(prompt), attachments=attachments)
    models = route.models

    last_exception = None

    available_models = [model for model in models if model_is_available(model)]
    if not available_models:
        raise RuntimeError("All configured Gemini models are temporarily unavailable; retry shortly.")

    for i, model_name in enumerate(available_models):
        logger.info(f"Attempting Bedrock model: {model_name}")

        try:
            response = await ai_observability.generate(
                client,
                model=model_name,
                contents=contents,
                config=config,
                feature=feature,
                submission_id=submission_id,
                route_tier=route.tier,
                route_reason=route.reason,
                attempt=i + 1,
            )

            if i > 0:
                logger.info(f"AI fallback succeeded with model: {model_name}")

            return response

        except Exception as e:
            last_exception = e
            if is_rotatable_model_error(e):
                record_model_failure(model_name, e)
                logger.warning(f"AI model {model_name} unavailable or quota exceeded ({e}). Moving to next model.")
                continue
            else:
                # Non-rotatable fatal error, re-raise immediately
                raise e

    logger.error("All Bedrock models are unavailable or out of quota")
    raise RuntimeError("All Bedrock models are unavailable or out of quota") from last_exception
