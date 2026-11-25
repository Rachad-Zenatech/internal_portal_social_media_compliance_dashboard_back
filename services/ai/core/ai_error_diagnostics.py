"""Safe, actionable descriptions of AI failures for users and server logs."""

import asyncio
import json

from services.shared.core.logging_service import request_id_ctx


def classify_error(error):
    """Classify known failures without returning raw provider messages or secrets."""
    seen = set()
    while error is not None and id(error) not in seen:
        seen.add(id(error))
        response = getattr(error, "response", {})
        code = response.get("Error", {}).get("Code", "") if isinstance(response, dict) else ""
        text = str(error).lower()
        if code in {"AccessDeniedException", "UnrecognizedClientException", "InvalidClientTokenId", "ExpiredTokenException"}:
            return "access_denied", "The AI provider rejected access. An administrator should check the service credentials and model permissions."
        if code in {"ThrottlingException", "TooManyRequestsException", "ServiceQuotaExceededException"} or type(error).__name__ == "QuotaExhausted":
            return "provider_quota", "The AI provider is rate-limiting requests or its quota has been reached. Try again later or ask an administrator to check the provider quota."
        if isinstance(error, (asyncio.TimeoutError, TimeoutError)) or type(error).__name__ in {"ReadTimeoutError", "ConnectTimeoutError"}:
            return "timeout", "The AI request timed out before a response arrived. Try again; if it continues, an administrator should check provider connectivity and server load."
        if "output token limit" in text or "max_tokens" in text:
            return "output_limit", "The AI response exceeded its output limit, so the result was incomplete. Try a smaller input or ask an administrator to review the AI output settings."
        if code in {"ResourceNotFoundException", "ValidationException"}:
            return "model_configuration", "The AI provider rejected the model or request configuration. An administrator should check the configured model and region."
        if code in {"ServiceUnavailableException", "InternalServerException", "ModelNotReadyException"}:
            return "provider_unavailable", "The AI provider is temporarily unavailable. Try again later."
        if isinstance(error, json.JSONDecodeError):
            return "invalid_response", "The AI provider returned an unreadable response. Try again; if it continues, ask an administrator to check the AI logs."
        error = error.__cause__ or error.__context__
    return "unknown", "The AI operation failed for an unknown reason. Ask an administrator to check the server logs."


def user_message(error):
    _, message = classify_error(error)
    request_id = request_id_ctx.get()
    return f"{message} Reference: {request_id}" if request_id else message
