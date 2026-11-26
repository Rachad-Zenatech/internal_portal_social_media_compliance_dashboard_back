"""Identify repeated source URLs without conflating different reports from one publisher."""
import re
from urllib.parse import parse_qsl, urlencode, urlsplit

_TRACKING = {"fbclid", "gclid", "dclid", "msclkid"}


def url_key(url):
    """Ignore transport, fragments and tracking; preserve article paths and identifying query values."""
    parts = urlsplit(url if re.match(r"^https?://", url, re.I) else "https://" + url)
    host = parts.netloc.casefold()
    if host.startswith("www."):
        host = host[4:]
    host = re.sub(r":(?:80|443)$", "", host)
    query = [(key, value) for key, value in parse_qsl(parts.query, keep_blank_values=True)
             if not key.casefold().startswith("utm_") and key.casefold() not in _TRACKING]
    path = parts.path.rstrip("/") or "/"
    return host + path + ("?" + urlencode(sorted(query)) if query else "")


def _replacement(citation, given):
    if not citation.truncated:
        return False
    key = url_key(citation.reference)
    prefix = re.split(r"\.{2,}|\u2026", key, maxsplit=1)[0]
    # A different report on the same site is not a replacement for an incomplete citation.
    matches = {url_key(c.reference) for c in given if not c.truncated and url_key(c.reference).startswith(prefix)}
    return len(matches) == 1


def citations(items):
    """Keep one citation per URL, preferring its explicitly attached source-link occurrence."""
    given = [c for c in items if c.kind == "URL" and c.where == "source_links"]
    selected = {}
    for citation in items:
        if citation.kind == "URL" and _replacement(citation, given):
            continue
        identity = (citation.kind, url_key(citation.reference) if citation.kind == "URL"
                    else " ".join(citation.reference.casefold().split()))
        current = selected.get(identity)
        if current is None or citation.where == "source_links":
            selected[identity] = citation
    return list(selected.values())


def readings(items):
    """Redirect aliases identify one report; an actually readable result wins over an unreadable alias."""
    selected = {}
    for check, material in items:
        identity = ("URL", url_key(check.url or check.reference)) if check.kind == "URL" else (
            check.kind, " ".join(check.reference.casefold().split()))
        current = selected.get(identity)
        if current is None or (check.status == "READ" and material is not None
                               and (current[0].status != "READ" or current[1] is None)):
            selected[identity] = check, material
    return list(selected.values())
