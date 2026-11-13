"""Settings for checking a post's sources, none of them fixed in code.

Site lists (which sites are official, the company's own, press-release wires or news) come from a JSON file,
`source_sites.json` next to this module or the one SOURCE_SITES_FILE names, and each list can be replaced by its
environment variable (comma separated). How the checker introduces itself and searches the web come from the
environment too.
"""

import json
import logging
import os
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Tuple

logger = logging.getLogger(__name__)

_DEFAULT_FILE = Path(__file__).with_name("source_sites.json")
# List name in the file -> environment variable that replaces it.
_LIST_ENV = {
    "official_endings": "SOURCE_OFFICIAL_ENDINGS",
    "official_sites": "SOURCE_OFFICIAL_SITES",
    "company_sites": "SOURCE_COMPANY_SITES",
    "company_site_words": "SOURCE_COMPANY_SITE_WORDS",
    "release_wires": "SOURCE_RELEASE_WIRES",
    "news_sites": "SOURCE_NEWS_SITES",
}

# How the checker names itself to the sites it reads and the search pages it asks.
USER_AGENT = os.getenv("SOURCE_FETCH_USER_AGENT", "Compliance-SourceCheck/1.0 (internal compliance tool)").strip()
# Google News search language and region, as its hl / gl / ceid parameters.
SEARCH_LANGUAGE = os.getenv("SOURCE_SEARCH_LANGUAGE", "en-US").strip()
SEARCH_REGION = os.getenv("SOURCE_SEARCH_REGION", "US").strip()
SEARCH_RESULTS = int(os.getenv("SOURCE_SEARCH_RESULTS", "5"))
SEARCH_HTTP_TIMEOUT_SEC = float(os.getenv("SOURCE_SEARCH_HTTP_TIMEOUT_SEC", "12"))
SEARCH_PAGE_TIMEOUT_MS = int(float(os.getenv("SOURCE_SEARCH_PAGE_TIMEOUT_SEC", "15")) * 1000)


@dataclass(frozen=True)
class SourceSites:
    official_endings: Tuple[str, ...] = ()
    official_sites: Tuple[str, ...] = ()
    company_sites: Tuple[str, ...] = ()
    company_site_words: Tuple[str, ...] = ()
    release_wires: Tuple[str, ...] = ()
    news_sites: Tuple[str, ...] = ()


def _clean(values) -> Tuple[str, ...]:
    out = []
    for v in values or []:
        v = str(v).strip().lower()
        v = v[4:] if v.startswith("www.") else v
        if v and v not in out:
            out.append(v)
    return tuple(out)


def _read_file() -> dict:
    path = Path(os.getenv("SOURCE_SITES_FILE") or _DEFAULT_FILE)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except FileNotFoundError:
        logger.warning("Source site list %s not found: every source will rank as OTHER.", path)
    except (OSError, ValueError) as e:
        logger.warning("Source site list %s could not be read (%s): every source will rank as OTHER.", path, e)
    return {}


@lru_cache(maxsize=1)
def sites() -> SourceSites:
    """The site lists, read once. `reload()` reads them again."""
    data = _read_file()
    lists = {}
    for name, env in _LIST_ENV.items():
        raw = os.getenv(env)
        lists[name] = _clean(raw.split(",") if raw is not None else data.get(name))
    return SourceSites(**lists)


def reload() -> SourceSites:
    sites.cache_clear()
    return sites()
