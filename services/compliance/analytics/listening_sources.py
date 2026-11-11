"""What to listen for about the company itself, besides the market topics (drone delivery, FAA rules...).

The market topics say what the industry is talking about; these say what is being said about *us*, in priority order:

    HIGH    company     the company's names
    HIGH    hashtags    campaign hashtags
    HIGH    products    product names
    MEDIUM  links       our website, wherever it is shared or cited
    MEDIUM  events      conference and launch names
    MEDIUM  competitors competitor brands and products

Each category that finds something becomes a listening topic ("watch-<category>"), so the rest of public mood (snapshots,
trends, matching a draft to it, evidence) works on it unchanged. The queries are in listening_queries.json, so the team can
add to them without code. Only what can be collected without credentials is here: news coverage. Replies to our posts and
quote posts need the X API or Sprout Listening, and are not part of this.
"""

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

PLAN_PATH = Path(os.getenv("LISTENING_QUERIES_PATH") or Path(__file__).with_name("listening_queries.json"))

# category -> (label shown as the topic name, priority, one line for the topic description)
CATEGORIES: Dict[str, Tuple[str, str, str]] = {
    "company": ("Brand names", "HIGH", "What is being said with the company's names."),
    "hashtags": ("Hashtags", "HIGH", "Coverage under our campaign hashtags."),
    "products": ("Product names", "HIGH", "What is being said about our products."),
    "links": ("Links to our site", "MEDIUM", "Articles and posts that cite our website."),
    "events": ("Events", "MEDIUM", "Conversation around our events and launches."),
    "competitors": ("Competitor mentions", "MEDIUM", "What is being said about competitors and their products."),
}


@dataclass
class Query:
    category: str
    text: str  # what a person would call it ("ZenaTech", "#ZooOffice", "zenatech.com")
    news: str  # the Google News search for it


def load_plan(path: Optional[Path] = None) -> Dict[str, Any]:
    try:
        plan = json.loads((path or PLAN_PATH).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return plan if isinstance(plan, dict) else {}


def build_queries(plan: Dict[str, Any]) -> List[Query]:
    """Every search the plan asks for. A category with nothing configured produces none (and is reported as such)."""
    out: List[Query] = []
    for category in CATEGORIES:
        for raw in plan.get(category) or []:
            text = str(raw).strip()
            if not text:
                continue
            # A hashtag or a cashtag symbol means nothing to a news search; the word does. A domain stays as it is.
            word = text.lstrip("#$@").strip()
            out.append(Query(category, text, f'"{word}"'))
    return out


def topic_for(category: str, queries: List[Query]) -> Dict[str, Any]:
    """The listening topic a category becomes: matched to drafts by the words it searches for."""
    label, priority, about = CATEGORIES[category]
    texts = list(dict.fromkeys(q.text.lstrip("#$@") for q in queries if q.category == category))
    return {
        "external_id": f"watch-{category}",
        "name": label,
        "description": f"[{priority}] {about}",
        "keywords": texts,
        "category": category,
        "priority": priority,
    }
