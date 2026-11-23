"""Extract wording examples used by rule self checks."""

import re
from typing import List, Optional, Tuple


_QUOTED = re.compile(r'"([^"]{4,})"|\u201c([^\u201d]{4,})\u201d')


def _examples(text: Optional[str], unquoted: bool) -> List[Tuple[str, bool]]:
    """(example, quoted) pairs from a rule's example text. Quoted examples are actual wording. Unquoted text is
    used too when `unquoted` (split at semicolons), since "not acceptable" examples are usually wording; it may
    describe imagery instead, which the result says."""
    if not text or not text.strip():
        return []
    quoted = [q.strip() for a, b in _QUOTED.findall(text) for q in [a or b] if q.strip()]
    if quoted:
        return [(q, True) for q in quoted]
    if not unquoted:
        return []
    return [(p.strip(), False) for p in text.split(";") if len(p.strip()) >= 4]
