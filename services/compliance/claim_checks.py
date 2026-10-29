"""Built-in claim checks: patterns a listed company's posts must be careful with, whatever the wording.

The rulebook matches exact phrases, so a post that says "expected to add $10M+ in revenue this year" slips past a
rule that looks for "revenue is expected". These checks look for the shape of a claim instead: money figures,
financial results paired with the future, unnamed customers, approvals with no issuer, status claims and contract
announcements. They run on every check (every brand belongs to the Nasdaq-listed company), cost no AI quota, and give
the same answer every time.

Each finding is reported like a rule's, with a fixed id so a reviewer can dismiss it the same way.
"""

import re
from dataclasses import dataclass
from typing import Callable, List
from uuid import NAMESPACE_URL, UUID, uuid5

from models.compliance_model import ComplianceViolation

# Bump when the checks change, so open content is re-checked on the next start (see rulebook_version).
VERSION = "claims-1"

PENALTY_HIGH = 20
PENALTY_MEDIUM = 10

# How far apart (in characters) a financial word and a future word can be and still read as one claim. Image text
# often breaks a sentence over lines ("EXPECTED TO ADD / $10M+ / IN REVENUE THIS YEAR"), so this spans a few lines.
FORWARD_WINDOW = 70
# How far around "approved" or "certified" to look for who gave it.
ISSUER_WINDOW = 60


@dataclass(frozen=True)
class ClaimCheck:
    key: str
    rule_number: str
    title: str
    category: str
    severity: str  # HIGH or MEDIUM
    explanation: str
    remediation: str
    find: Callable[[str], List[str]]

    @property
    def rule_id(self) -> UUID:
        return uuid5(NAMESPACE_URL, f"zenatech-claim-check:{self.key}")

    @property
    def penalty(self) -> int:
        return PENALTY_HIGH if self.severity == "HIGH" else PENALTY_MEDIUM


def _clean(text: str) -> str:
    return " ".join(text.lower().split())


def _snippets(pattern: "re.Pattern[str]", text: str) -> List[str]:
    """Each match once, in order, trimmed of surrounding spaces and punctuation."""
    out: List[str] = []
    for m in pattern.finditer(text):
        s = m.group(0).strip(" .,;:!?/-")
        if s and s not in out:
            out.append(s)
    return out


# --- Money figures ---------------------------------------------------------------------------------------------

_MONEY = re.compile(
    r"(?:\b(?:c|ca|us|au))?\$\s?\d[\d,]*(?:\.\d+)?\s?(?:k|m|mm|b|bn|million|billion|thousand)?\b\+?"
    r"|\b\d[\d,]*(?:\.\d+)?\s?(?:million|billion)\b"
    r"|\b\d+(?:\.\d+)?\s?%(?:\W+\w+){0,2}?\W+(?:growth|increase|revenue|revenues|margin|margins|return|returns|sales)\b"
)


def _money(text: str) -> List[str]:
    return _snippets(_MONEY, text)


# --- Forward-looking financial statements ----------------------------------------------------------------------

_FINANCIAL = re.compile(
    r"\b(?:revenue|revenues|sales|earnings|profit|profits|profitability|income|ebitda|margin|margins|growth|"
    r"bookings|backlog|guidance|cash flow|run rate|arr)\b"
)
_FUTURE = re.compile(
    r"\b(?:expected to|expects to|expect to|we expect|is expected|are expected|will|anticipate[sd]?|"
    r"projected|projects|projecting|forecast(?:s|ed)?|estimate[sd]?|on track|this year|next year|this quarter|"
    r"next quarter|by year end|by the end of (?:the|this) year|in 20[2-9]\d)\b"
)
_SAFE_HARBOR = re.compile(r"forward\W+looking")


def _forward_looking(text: str) -> List[str]:
    if _SAFE_HARBOR.search(text):
        return []
    found: List[str] = []
    futures = list(_FUTURE.finditer(text))
    for fin in _FINANCIAL.finditer(text):
        # The nearest future word on either side, within the window.
        near = [f for f in futures if abs(f.start() - fin.start()) <= FORWARD_WINDOW]
        if not near:
            continue
        fut = min(near, key=lambda f: abs(f.start() - fin.start()))
        s = text[min(fin.start(), fut.start()): max(fin.end(), fut.end())].strip(" .,;:!?/-")
        if s and s not in found:
            found.append(s)
    return found


# --- Unnamed customers and partners ----------------------------------------------------------------------------

_UNNAMED = re.compile(
    r"\b(?:major|leading|large|big|top|global|international|multinational|fortune (?:500|100)|tier[- ]?1|"
    r"well[- ]known|prominent|household[- ]name|blue[- ]chip)"
    r"(?:\s+(?:global|international|multinational|u\.?s\.?|american|canadian|european|asian|tech|technology|"
    r"defense|energy|telecom|logistics|industrial|healthcare|financial))*"
    r"\s+(?:company|companies|customer|customers|client|clients|partner|partners|enterprise|enterprises|"
    r"corporation|corporations|firm|firms|oem|oems|retailer|retailers|bank|banks|carrier|carriers|operator|"
    r"operators|manufacturer|manufacturers|organization|organisation|buyer|buyers)\b"
)


def _unnamed(text: str) -> List[str]:
    return _snippets(_UNNAMED, text)


# --- Approvals and certifications with no issuer --------------------------------------------------------------

_APPROVAL = re.compile(
    r"\b(?:(?:officially|fully|formally|government|regulatory)\s+)?(?:approved|certified|authori[sz]ed|accredited)\b"
)
_ISSUER = re.compile(
    r"\b(?:faa|transport canada|easa|fcc|dod|department|ministry|agency|administration|government|regulator|"
    r"nasdaq|sec|iso|health canada|ul|part 107|waiver|board|shareholders|commission|authority)\b"
)


def _approval(text: str) -> List[str]:
    found: List[str] = []
    for m in _APPROVAL.finditer(text):
        around = text[max(0, m.start() - ISSUER_WINDOW): m.end() + ISSUER_WINDOW]
        if _ISSUER.search(around):
            continue
        s = m.group(0)
        if s not in found:
            found.append(s)
    return found


# --- Deployment and operational status -------------------------------------------------------------------------

_STATUS = re.compile(
    r"\b(?:already|now|currently|fully|actively)\s+(?:in\s+(?:deployment|production|operation|service|use)|"
    r"deployed|operational|rolled\s+out|in\s+the\s+field)\b"
    r"|\bin\s+(?:full\s+)?deployment\b"
)


def _status(text: str) -> List[str]:
    return _snippets(_STATUS, text)


# --- Contract and deal announcements ---------------------------------------------------------------------------

_CONTRACT = re.compile(
    r"\b(?:signed|awarded|won|secured|landed|inked|closed)\b(?:\W+[\w$]+){0,5}?\W+"
    r"(?:contract|contracts|deal|deals|agreement|agreements|purchase order|purchase orders|award)\b"
    r"|\b(?:contract|deal|agreement)\s+(?:value|worth|valued at)\b"
)


def _contract(text: str) -> List[str]:
    return _snippets(_CONTRACT, text)


CHECKS: List[ClaimCheck] = [
    ClaimCheck(
        key="forward-looking-financial",
        rule_number="Claim check 1",
        title="Forward-looking financial statement without safe-harbor wording",
        category="Financial Disclosure",
        severity="HIGH",
        explanation="Revenue, earnings or growth is tied to the future (expected, will, this year), which is a "
        "forward-looking statement, and the post doesn't carry safe-harbor wording.",
        remediation="Use only figures already in a published press release or filing, and add: \"This post contains "
        "forward-looking statements; actual results may differ materially.\" Or remove the projection.",
        find=_forward_looking,
    ),
    ClaimCheck(
        key="contract-announcement",
        rule_number="Claim check 2",
        title="Contract or deal announcement",
        category="Financial Disclosure",
        severity="HIGH",
        explanation="Announcing a signed or won contract is a material event for a listed company. It may only be "
        "posted once it's been publicly announced, worded as in that announcement.",
        remediation="Link the press release, match its wording and figures exactly, and have Investor Relations "
        "confirm before posting.",
        find=_contract,
    ),
    ClaimCheck(
        key="unqualified-approval",
        rule_number="Claim check 3",
        title="Approval or certification with no issuer",
        category="Regulatory Claims",
        severity="HIGH",
        explanation="'Approved' or 'certified' without saying who approved it, for what and when overstates the "
        "status and can't be verified.",
        remediation="Name the issuer and scope exactly as in the document (e.g. \"FAA Part 107 waiver for ...\"), "
        "or remove the claim.",
        find=_approval,
    ),
    ClaimCheck(
        key="financial-figure",
        rule_number="Claim check 4",
        title="Financial figure",
        category="Financial Disclosure",
        severity="MEDIUM",
        explanation="Dollar amounts and growth percentages must match a public filing or press release exactly.",
        remediation="Check every figure against the published source and keep the source with the post; remove "
        "any figure that hasn't been published.",
        find=_money,
    ),
    ClaimCheck(
        key="unnamed-counterparty",
        rule_number="Claim check 5",
        title="Unnamed customer or partner",
        category="Substantiation",
        severity="MEDIUM",
        explanation="Describing a customer or partner as 'a major global company' can't be verified and suggests "
        "more than may be disclosed.",
        remediation="Name the customer if the announcement does, using its wording; otherwise describe the work "
        "without characterising the customer.",
        find=_unnamed,
    ),
    ClaimCheck(
        key="deployment-status",
        rule_number="Claim check 6",
        title="Deployment or operational status claim",
        category="Development Status",
        severity="MEDIUM",
        explanation="Saying something is already deployed, in production or operational must match its documented "
        "status.",
        remediation="Use the exact stage (in testing, piloted, available) and keep evidence of it on file.",
        find=_status,
    ),
]


FORWARD_LOOKING_ID = CHECKS[0].rule_id


def find_claims(text: str) -> List[ComplianceViolation]:
    """The built-in claim checks that fire on this text, as findings."""
    clean = _clean(text)
    out: List[ComplianceViolation] = []
    for check in CHECKS:
        hits = check.find(clean)
        if not hits:
            continue
        out.append(ComplianceViolation(
            rule_id=check.rule_id,
            rule_number=check.rule_number,
            title=check.title,
            category=check.category,
            severity=check.severity,
            traffic_light="YELLOW",
            flagged_terms=hits[:8],
            explanation=f"Found: {', '.join(f'\"{h}\"' for h in hits[:4])}. {check.explanation}",
            remediation_guidance=check.remediation,
            penalty=check.penalty,
        ))
    return out
