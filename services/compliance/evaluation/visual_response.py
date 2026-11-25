"""A bounded larger retry for cut-off visual assessments; never assume missing coverage passed."""
import os


async def read(generate, parse):
    initial = max(2048, min(int(os.getenv('VISUAL_COMPLIANCE_MAX_OUTPUT_TOKENS', '4096')), 16384))
    last = None
    for limit in (initial, min(initial * 2, 32768)):
        try:
            response = await generate(limit)
            if getattr(response, 'stop_reason', None) == 'max_tokens':
                raise ValueError('Visual assessment reached its response limit.')
            return response, parse(response)
        except (ValueError, TypeError) as error:
            last = error
    raise ValueError('Visual assessment could not finish after a larger response retry.') from last
