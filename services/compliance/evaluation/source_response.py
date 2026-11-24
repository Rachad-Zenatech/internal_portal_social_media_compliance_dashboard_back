"""Bound source-response retries and distinguish truncated answers from model outages."""
import json
import os
from services.ai.core import ai_types as types


class IncompleteSourceResponse(ValueError):
    def __init__(self, code):
        self.code = code
        super().__init__('Source comparison exceeded its response limit.' if code == 'OUTPUT_LIMIT' else 'Source comparison returned an incomplete response.')


def parse(response, response_type, limit):
    used = getattr(getattr(response, 'usage_metadata', None), 'candidates_token_count', None)
    if getattr(response, 'stop_reason', None) == 'max_tokens':
        raise IncompleteSourceResponse('OUTPUT_LIMIT')
    try:
        data = json.loads(response.text or '{}')
    except (ValueError, TypeError) as error:
        code = 'OUTPUT_LIMIT' if isinstance(used, (int, float)) and used >= limit else 'INVALID_RESPONSE'
        raise IncompleteSourceResponse(code) from error
    if isinstance(data, list):
        data = {'claims': data}
    if not isinstance(data, dict) or not isinstance(data.get('claims'), list):
        raise IncompleteSourceResponse('INVALID_RESPONSE')
    return response_type.model_validate(data)


async def read(generate, response_type, *, system_prompt, schema, timeout):
    initial = max(2048, min(int(os.getenv('SOURCE_CHECK_MAX_OUTPUT_TOKENS', '8192')), 16384))
    config = types.GenerateContentConfig(system_instruction=system_prompt, response_mime_type='application/json',
        response_schema=schema, max_output_tokens=initial, temperature=0,
        http_options=types.HttpOptions(timeout=timeout * 1000))
    last = None
    for attempt in range(2):
        try:
            response = await generate(config, attempt + 1)
            return parse(response, response_type, config.max_output_tokens)
        except ValueError as error:
            last = error if isinstance(error, IncompleteSourceResponse) else IncompleteSourceResponse('INVALID_RESPONSE')
            config.max_output_tokens = min(initial * 2, 32768)
    raise last
