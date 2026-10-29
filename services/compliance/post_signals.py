"""Post signals: the parts of a post that carry meaning in their symbols, read before the rule matcher strips them.

The rule matcher lowercases text and turns punctuation into spaces, so "$ZENA" reads as "zena", "#CombatProven" as
"combatproven" and "@USArmy" as "usarmy". That hides three things a reviewer cares about:

- cashtags ($ZENA): the post talks about the company's stock, so investors are reading it;
- hashtags: "#CombatProven" says "combat proven" just as loudly as the words do;
- @mentions and links: tagging a government or military account, or linking to a .gov / .mil site, suggests the
  government is associated with or endorses the post, which the rulebook forbids without a disclaimer.

This module pulls those out of the raw text. It's plain pattern matching: no AI quota, same answer every time.
"""

import re
from dataclasses import dataclass, field
from typing import List, Sequence
from uuid import NAMESPACE_URL, UUID, uuid5

# Bump when what's read or flagged here changes, so open content is re-checked on the next start (see
# compliance_service.rulebook_version).
VERSION = "signals-1"

# The brand guidance says 3-5 hashtags; more than this reads as a hashtag wall.
MAX_HASHTAGS = 5

# --- Patterns over the raw text ---------------------------------------------------------------------------------

# $ZENA, $ZenaTech. Starts with a letter, so money ("$25M") isn't a cashtag.
_CASHTAG = re.compile(r"(?<![\w$])\$([A-Za-z][A-Za-z.]{0,14})(?![\w])")
# #CombatProven, #drone_tech. Starts with a letter, so "#1" isn't a hashtag; "&#39;" (HTML) isn't either.
_HASHTAG = re.compile(r"(?<![\w#&])#([A-Za-z][A-Za-z0-9_]{0,99})")
# @USArmy, @zena.drone. Not the middle of an email address (no word character or dot before the @).
_MENTION = re.compile(r"(?<![\w@.])@([A-Za-z0-9_](?:[A-Za-z0-9_.]{0,48}[A-Za-z0-9_])?)")
# Links with a scheme or "www.", and bare domains on the usual endings ("zenatech.com/news", "army.mil").
_URL = re.compile(
    r"(?<![@\w.-])(?:https?://|www\.)[^\s<>\"'()\[\]]+"
    r"|(?<![@\w./-])[a-z0-9][a-z0-9-]*(?:\.[a-z0-9-]+)*\.(?:com|org|net|gov|mil|ca|io|ai|co|us|uk|int|news)"
    r"(?:/[^\s<>\"'()\[\]]*)?(?![\w-])",
    re.IGNORECASE,
)
# CamelCase words inside a hashtag: "CombatProven" -> "Combat", "Proven"; capital runs stay together
# ("UAVTech" -> "UAV", "Tech").
_CAMEL = re.compile(r"[A-Z]+(?=[A-Z][a-z])|[A-Z]?[a-z]+|[A-Z]+|\d+")
# The rulebook's "no endorsement" wording, in any of its usual forms.
_NO_ENDORSEMENT = re.compile(r"\b(?:not|no|nor)\b(?!\s+only\b)[^.\n]{0,80}\bendors", re.IGNORECASE)

# --- Government and military accounts and sites -------------------------------------------------------------------

# Handles that are government or military accounts. Compared without "_" or ".", lowercase.
_GOV_HANDLES = {
    # United States
    "dod", "deptofdefense", "deptofwar", "secdef", "secwar", "dodcio", "dodresearch", "dodiaa", "darpa", "diux",
    "usarmy", "usnavy", "usairforce", "usmc", "marines", "spaceforcedod", "usspaceforce", "nationalguard", "uscg",
    "usnorthcom", "uscentcom", "indopacom", "socom", "ussocom", "afrl", "armyfutures", "navalresearch",
    "faa", "faanews", "secgov", "whitehouse", "statedept", "dhsgov", "nasa", "dhs", "cbp", "usdot",
    # Canada
    "canadianforces", "forcesgc", "nationaldefence", "natdefence", "dnd", "rcaf", "rcn", "canadianarmy",
    "transportgc", "transportcanada",
    # United Kingdom, NATO, allies
    "defencehq", "modgovuk", "britisharmy", "royalnavy", "royalairforce", "nato", "natonews", "eda",
}
# Words that mark a handle as military or defense-ministry wherever they appear in it ("@USArmyReserve").
_GOV_HANDLE_WORDS = ("army", "navy", "airforce", "spaceforce", "marinecorps", "pentagon", "deptof", "nationalguard",
                     "coastguard", "ministryofdefence", "defencegov")
# Handles containing those words that aren't the military.
_NOT_GOV_HANDLES = ("salvationarmy", "swissarmy", "navyblue", "navyfederal", "oldnavy", "armyofgod")
# Domains that belong to governments and militaries.
_GOV_DOMAIN_ENDINGS = (".gov", ".mil", ".gc.ca", ".gov.uk", ".mod.uk", "nato.int", ".gov.au", ".mil.ca")

# Our own accounts and sites ("@ZenaDroneDefense", "zenatech.com") are never flagged or counted as outside links.
_OWN_MARKER = "zena"


def _key(handle_or_domain: str) -> str:
    return re.sub(r"[_.\-]", "", handle_or_domain.lower())


def is_government_handle(handle: str) -> bool:
    k = _key(handle)
    if not k or _OWN_MARKER in k or any(n in k for n in _NOT_GOV_HANDLES):
        return False
    return k in _GOV_HANDLES or any(w in k for w in _GOV_HANDLE_WORDS)


def _domain(link: str) -> str:
    d = re.sub(r"^(?:https?://)?(?:www\.)?", "", link.strip().lower())
    return d.split("/", 1)[0].split("?", 1)[0].split("#", 1)[0].rstrip(".,;:!?")


def is_government_domain(domain: str) -> bool:
    d = domain.lower()
    return any(d.endswith(e) or d == e.lstrip(".") for e in _GOV_DOMAIN_ENDINGS)


def is_own_domain(domain: str) -> bool:
    return _OWN_MARKER in domain.lower()


def hashtag_words(tag: str) -> str:
    """'CombatProven' -> 'combat proven'. A tag written all in one case can't be split and comes back as is."""
    words = _CAMEL.findall(tag.replace("_", " "))
    return " ".join(w.lower() for w in words) if len(words) > 1 else tag.lower().replace("_", " ")


def _unique(items: Sequence[str]) -> List[str]:
    return list(dict.fromkeys(i for i in items if i))


@dataclass
class PostSignals:
    cashtags: List[str] = field(default_factory=list)       # "$ZENA", as written
    hashtags: List[str] = field(default_factory=list)       # "#CombatProven", as written
    mentions: List[str] = field(default_factory=list)       # "@USArmy", as written
    links: List[str] = field(default_factory=list)          # domains: "army.mil", "zenatech.com"
    government_references: List[str] = field(default_factory=list)  # the mentions and domains above that are gov/mil
    outside_links: List[str] = field(default_factory=list)  # domains that are neither ours nor gov/mil
    has_no_endorsement_wording: bool = False

    @property
    def hashtag_keys(self) -> List[str]:
        """Hashtags as the matcher sees them: lowercase, no '#' or '_' ("#Combat_Proven" -> "combatproven")."""
        return _unique([_key(h.lstrip("#")) for h in self.hashtags])

    @property
    def hashtag_phrases(self) -> List[str]:
        """Hashtags with their CamelCase words split out ("#CombatProven" -> "combat proven"), for phrase rules.
        Only tags that actually split are returned; one-word tags are already matched as they are."""
        return _unique([p for p in (hashtag_words(h.lstrip("#")) for h in self.hashtags) if " " in p])

    def notes(self) -> List[str]:
        """Things a reviewer should look at that aren't rule breaches, so they cost no points."""
        out: List[str] = []
        if self.outside_links:
            out.append(
                f"Links to outside sites ({', '.join(self.outside_links[:5])}): check each one goes where it should "
                "and is cleared to share."
            )
        if len(self.hashtags) > MAX_HASHTAGS:
            out.append(f"{len(self.hashtags)} hashtags: the brand guidance is 3 to 5, more reads as a hashtag wall.")
        return out


def extract(text: str) -> PostSignals:
    """Cashtags, hashtags, mentions and links in the post, each once, in the order they appear."""
    text = text or ""
    mentions = _unique([f"@{m}" for m in _MENTION.findall(text)])
    domains = _unique([_domain(u) for u in _URL.findall(text)])
    gov = [m for m in mentions if is_government_handle(m[1:])]
    gov += [d for d in domains if is_government_domain(d) and not is_own_domain(d)]
    return PostSignals(
        cashtags=_unique([f"${c.rstrip('.')}" for c in _CASHTAG.findall(text)]),
        hashtags=_unique([f"#{h}" for h in _HASHTAG.findall(text)]),
        mentions=mentions,
        links=domains,
        government_references=gov,
        outside_links=[d for d in domains if not is_own_domain(d) and not is_government_domain(d)],
        has_no_endorsement_wording=bool(_NO_ENDORSEMENT.search(text)),
    )


# --- Findings -----------------------------------------------------------------------------------------------------

GOVERNMENT_REFERENCE_ID: UUID = uuid5(NAMESPACE_URL, "zenatech-signal-check:government-reference")
GOVERNMENT_REFERENCE_PENALTY = 20


@dataclass(frozen=True)
class SignalFinding:
    rule_id: UUID
    rule_number: str
    title: str
    category: str
    severity: str
    traffic_light: str
    flagged_terms: List[str]
    explanation: str
    remediation: str
    penalty: int


def findings(signals: PostSignals) -> List[SignalFinding]:
    """Rule-like findings from the signals. Reported like a rule's, with a fixed id so a reviewer can dismiss them."""
    out: List[SignalFinding] = []
    # Tagging the DoD (or linking its site) with the "no endorsement" wording already in the post is what the
    # rulebook asks for, so it isn't flagged again.
    if signals.government_references and not signals.has_no_endorsement_wording:
        refs = signals.government_references
        out.append(SignalFinding(
            rule_id=GOVERNMENT_REFERENCE_ID,
            rule_number="Signal check 1",
            title="Government or military account tagged, or site linked",
            category="Endorsement",
            severity="HIGH",
            traffic_light="YELLOW",
            flagged_terms=refs[:8],
            explanation=f"Found: {', '.join(refs[:4])}. Tagging a government or military account, or linking to its "
            "site, suggests it is associated with or endorses this post. Government and military visuals and "
            "references may not imply endorsement.",
            remediation="Remove the tag or link, or keep it with clearance and add: \"The appearance of U.S. "
            "Department of Defense visual information does not imply or constitute DOD endorsement.\"",
            penalty=GOVERNMENT_REFERENCE_PENALTY,
        ))
    return out
