"""Validate completed sentiment output without mistaking truncation for an empty assessment."""
import json

from models.compliance.sentiment_model import SentimentResult


class OutputLimitReached(ValueError):
    """The provider used its output budget before completing the assessment."""


def parse_response(response, token_limit):
    text = (response.text or "").strip()
    used = getattr(getattr(response, "usage_metadata", None), "candidates_token_count", None)
    if getattr(response, "stop_reason", None) == "max_tokens" or (
        not text and isinstance(used, (int, float)) and used >= token_limit
    ):
        raise OutputLimitReached("The sentiment response reached its output limit before finishing.")
    if not text:
        raise ValueError("The sentiment model returned no assessment text.")
    return SentimentResult.model_validate(json.loads(text))
