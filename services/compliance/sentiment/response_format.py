"""Validate completed sentiment output without mistaking truncation for an empty assessment."""
import json

from pydantic import ValidationError
from models.compliance.sentiment_model import SentimentResult
from services.ai.core.response_errors import InvalidResponseError, OutputLimitError

# Preserve the existing public import while recognizing provider-side truncation too.
OutputLimitReached = OutputLimitError


def parse_response(response, token_limit):
    text = (response.text or "").strip()
    used = getattr(getattr(response, "usage_metadata", None), "candidates_token_count", None)
    if getattr(response, "stop_reason", None) == "max_tokens" or (
        not text and isinstance(used, (int, float)) and used >= token_limit
    ):
        raise OutputLimitReached("The sentiment response reached its output limit before finishing.",
                                 token_limit=token_limit, response=response)
    if not text:
        raise InvalidResponseError("The sentiment model returned no assessment text.")
    try:
        return SentimentResult.model_validate(json.loads(text))
    except json.JSONDecodeError as exc:
        if isinstance(used, (int, float)) and used >= token_limit:
            raise OutputLimitReached(token_limit=token_limit, response=response) from exc
        raise InvalidResponseError("Sentiment returned malformed JSON.") from exc
    except ValidationError as exc:
        # Log field paths/types, never the post text or provider's raw response.
        fields = ", ".join(".".join(map(str, e["loc"])) + ":" + e["type"]
                           for e in exc.errors(include_input=False, include_url=False)[:8])
        raise InvalidResponseError("Sentiment assessment failed validation: " + fields) from exc
