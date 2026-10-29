"""Hashtag library: the hashtags (and the company's cashtag) a post can use, for its brand, audience and platforms.

One list, used in two places so they always agree: the Hashtags picker on the new-submission page, and the caption
suggestions, which pick from it first. Every tag is screened against the rules that apply to the post, so neither
offers a hashtag the rulebook would flag.

The cashtag ($ZENA) is offered on its own, with a note: it puts the post in front of investors, so investor-disclosure
rules apply to everything the post says. That's a choice the writer makes, not a default.
"""

from typing import Dict, Iterable, List, Optional, Sequence

from pydantic import BaseModel, Field

# The company's Nasdaq ticker as a cashtag.
CASHTAG = "$ZENA"
CASHTAG_NOTE = "Reaches investors following the stock. Investor-disclosure rules then apply to the whole post."

# How many hashtags suit each platform. Reddit and websites don't use them.
PLATFORM_MAX: Dict[str, int] = {
    "X": 2,
    "LINKEDIN": 3,
    "FACEBOOK": 3,
    "YOUTUBE": 3,
    "YOUTUBE_SHORTS": 3,
    "TIKTOK": 4,
    "INSTAGRAM": 5,
    "REDDIT": 0,
    "WEBSITE": 0,
}
DEFAULT_MAX = 3

PLATFORM_LABELS: Dict[str, str] = {
    "X": "X", "LINKEDIN": "LinkedIn", "FACEBOOK": "Facebook", "YOUTUBE": "YouTube", "YOUTUBE_SHORTS": "YouTube Shorts",
    "TIKTOK": "TikTok", "INSTAGRAM": "Instagram", "REDDIT": "Reddit", "WEBSITE": "the website",
}

BRAND_TAGS: Dict[str, List[str]] = {
    "ZENATECH": ["#ZenaTech", "#DroneTech", "#EnterpriseAI", "#Drones"],
    "ZENADRONE": ["#ZenaDrone", "#Drones", "#AutonomousSystems", "#UAS"],
    "DAAS": ["#DroneAsAService", "#DaaS", "#DroneInspection", "#ZenaTech"],
    "SOFTWARE": ["#ZooOffice", "#SaaS", "#FutureOfWork", "#ZenaTech"],
    "CEO_THOUGHT_LEADERSHIP": ["#Leadership", "#DefenseTech", "#Autonomy", "#ZenaTech"],
}
FALLBACK_BRAND_TAGS = ["#ZenaTech", "#ZenaDrone", "#Drones", "#DroneTech"]

AUDIENCE_TAGS: Dict[str, List[str]] = {
    "DEFENSE": ["#DefenseTech", "#DefenseInnovation", "#UAS"],
    "COMMERCIAL": ["#CommercialDrones", "#DroneInspection", "#PrecisionAg"],
    "INVESTORS": ["#ZenaTech", "#Nasdaq"],
    "GENERAL": ["#Drones", "#Technology", "#ZenaTech"],
}
AUDIENCE_LABELS: Dict[str, str] = {
    "DEFENSE": "Defense & Government", "COMMERCIAL": "Commercial Customers", "INVESTORS": "Investors",
    "GENERAL": "General Audience",
}


class LibraryTag(BaseModel):
    tag: str
    # Why to use it, or what using it changes (the cashtag).
    note: Optional[str] = None


class LibraryGroup(BaseModel):
    label: str
    tags: List[LibraryTag] = Field(default_factory=list)


class HashtagLibrary(BaseModel):
    groups: List[LibraryGroup] = Field(default_factory=list)
    # How many hashtags to use at most: the lowest of the selected platforms'.
    max_hashtags: int = DEFAULT_MAX
    # e.g. "Up to 2 for X". Empty when no platform is selected.
    max_reason: Optional[str] = None
    cashtag: str = CASHTAG


def max_hashtags(platforms: Sequence[str]) -> int:
    """The most hashtags the strictest selected platform suits (3 when none is selected)."""
    limits = [PLATFORM_MAX[p] for p in platforms if p in PLATFORM_MAX]
    return min(limits) if limits else DEFAULT_MAX


def max_reason(platforms: Sequence[str]) -> Optional[str]:
    known = [p for p in platforms if p in PLATFORM_MAX]
    if not known:
        return None
    strictest = min(known, key=lambda p: PLATFORM_MAX[p])
    n = PLATFORM_MAX[strictest]
    label = PLATFORM_LABELS.get(strictest, strictest)
    return f"No hashtags on {label}" if n == 0 else f"Up to {n} for {label}"


def _unique(tags: Iterable[str]) -> List[str]:
    seen, out = set(), []
    for t in tags:
        if t.lower() not in seen:
            seen.add(t.lower())
            out.append(t)
    return out


def candidates(brand_code: Optional[str], audience: Optional[str]) -> List[str]:
    """Every hashtag the library offers this brand and audience, brand tags first (no cashtag)."""
    return _unique([*BRAND_TAGS.get(brand_code or "", FALLBACK_BRAND_TAGS), *AUDIENCE_TAGS.get(audience or "", [])])


def build(brand_code: Optional[str], audience: Optional[str], platforms: Sequence[str],
          allowed: Optional[Iterable[str]] = None) -> HashtagLibrary:
    """The library for one post. `allowed` (if given) is the subset of candidates the rulebook doesn't flag."""
    ok = {t.lower() for t in allowed} if allowed is not None else None

    def keep(tags: List[str]) -> List[str]:
        return [t for t in tags if ok is None or t.lower() in ok]

    brand_tags = keep(BRAND_TAGS.get(brand_code or "", FALLBACK_BRAND_TAGS))
    audience_tags = [t for t in keep(AUDIENCE_TAGS.get(audience or "", [])) if t.lower() not in {b.lower() for b in brand_tags}]
    groups = [LibraryGroup(label="Brand", tags=[LibraryTag(tag=t) for t in brand_tags])]
    if audience_tags:
        groups.append(LibraryGroup(label=f"For {AUDIENCE_LABELS.get(audience or '', 'this audience')}",
                                   tags=[LibraryTag(tag=t) for t in audience_tags]))
    groups.append(LibraryGroup(label="Investors", tags=[LibraryTag(tag=CASHTAG, note=CASHTAG_NOTE)]))
    return HashtagLibrary(
        groups=[g for g in groups if g.tags],
        max_hashtags=max_hashtags(platforms),
        max_reason=max_reason(platforms),
    )
