"""Read complete image-text JSON, with one bounded retry for truncated or malformed output."""
import json
import os


def parse(response):
    if getattr(response, "stop_reason", None) == "max_tokens":
        raise ValueError("The image-text response reached its output limit.")
    result = json.loads(response.text or "{}")
    if not isinstance(result, dict) or not isinstance(result.get("text"), str):
        raise ValueError("The image-text response did not contain a text field.")
    for name in ('person_detected', 'ai_generated'):
        if name in result and not isinstance(result[name], bool):
            raise ValueError(f'The image-text response has an invalid {name} field.')
    if 'faces' in result and not isinstance(result['faces'], list):
        raise ValueError('The image-text response has an invalid faces field.')
    return result


async def read(generate, contents):
    initial = max(1024, min(int(os.getenv("IMAGE_TEXT_MAX_OUTPUT_TOKENS", "8192")), 16384))
    for limit in (initial, min(initial * 2, 32768)):
        try:
            response = await generate(contents=contents, max_output_tokens=limit)
            return parse(response)
        except (ValueError, TypeError):
            continue
    raise ValueError("The image reader returned an incomplete response twice. Use Check again to retry the image read.")
