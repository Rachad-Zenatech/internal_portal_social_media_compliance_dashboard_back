"""Compliance Rules & Governance Service
Rule CRUD and rulebook screening of captions, scripts and transcripts.
"""

import logging
import os
import re
from dataclasses import dataclass
from functools import lru_cache
from typing import Any, Dict, List, Optional, Sequence, Tuple
from uuid import UUID

import asyncpg
from fastapi import HTTPException

from models.compliance.compliance_model import (
    AddedRuleSet,
    CheckSignals,
    ComplianceCheckRequest,
    ComplianceCheckResponse,
    ComplianceRule,
    ComplianceRuleCreate,
    ComplianceRuleUpdate,
    ComplianceViolation,
    ManualReviewRule,
    RuleExampleResult,
    RuleSelfTest,
)
from models.compliance.submission_model import AUDIENCES, PLATFORMS, REGIONS, RULE_SETS
from postgresql_db.database import fetch_all, fetch_one, get_pool
from services.compliance import claim_checks, post_signals, reference_index_service
from services.shared.audit_service import log_audit_event
from services.shared.cache_service import emit_cache_invalidation, rules_cache

logger = logging.getLogger(__name__)

SCOPE_FIELDS = ("rule_sets", "brands", "audiences", "regions", "platforms")
# Region a submission uses for "posted everywhere"; it matches every region-scoped rule.
GLOBAL_REGION = "Global"
INVESTOR_AUDIENCE = "INVESTORS"
DEFENSE_AUDIENCE = "DEFENSE"
DISCLOSURE_RULE_SET = "Public Company Disclosure"
DEFENSE_RULE_SET = "ZenaDrone Defense"
CEO_BRAND = "CEO_THOUGHT_LEADERSHIP"
CEO_RULE_SET = "Shaun CEO Thought Leadership"
# What a rule's "applies to" can name. "caption" is the post's text, which every check reads.
CONTENT_PARTS = ("video", "imagery", "caption", "ceo_post")
# How long the rulebook stays cached. Rule changes made here drop it at once on every server (cache invalidation
# over Postgres NOTIFY); this only bounds changes made around the app, e.g. a schema/seed script run by hand.
RULES_CACHE_TTL = int(os.getenv("RULES_CACHE_TTL_SECONDS", "300"))
# Bump when how rules are picked or matched changes, so open content is re-checked on the next start.
# 3: near-match disclaimers must be together; any finding is at least YELLOW.
# 4: investor and defense wording brings in the Public Company Disclosure and ZenaDrone Defense rule sets.
SCOPE_VERSION = "scope-4"


def _row_to_rule(row: dict) -> ComplianceRule:
    return ComplianceRule(
        id=row["id"],
        rulebook_id=row.get("rulebook_id"),
        rule_number=row["rule_number"],
        category=row["category"],
        title=row["title"],
        summary=row["summary"],
        severity=row["severity"],
        traffic_light=row["traffic_light"],
        prohibited_terms=list(row.get("prohibited_terms") or []),
        required_disclaimers=list(row.get("required_disclaimers") or []),
        disclaimer_triggers=list(row.get("disclaimer_triggers") or []),
        exception_terms=list(row.get("exception_terms") or []),
        allowed_examples=row.get("allowed_examples"),
        prohibited_examples=row.get("prohibited_examples"),
        remediation_guidance=row.get("remediation_guidance"),
        applies_to=list(row.get("applies_to") or ["video", "imagery", "caption"]),
        **{f: list(row.get(f) or []) for f in SCOPE_FIELDS},
        is_active=row["is_active"],
        is_custom=row.get("is_custom", False),
        sort_order=row.get("sort_order", 0),
        created_at=row["created_at"],
        updated_at=row["updated_at"],
        created_by=row.get("created_by"),
        updated_by=row.get("updated_by"),
    )


async def _clean_scope(values: Dict[str, Optional[List[str]]]) -> Dict[str, List[str]]:
    """Validate scope lists against the known rule sets, brands, audiences, regions and platforms."""
    allowed: Dict[str, Any] = {
        "rule_sets": RULE_SETS,
        "audiences": list(AUDIENCES),
        "regions": REGIONS,
        "platforms": PLATFORMS,
    }
    if values.get("brands"):
        allowed["brands"] = [r["code"] for r in await fetch_all("SELECT code FROM brands WHERE is_active")]

    cleaned: Dict[str, List[str]] = {}
    for field, vals in values.items():
        if vals is None:
            continue
        vals = list(dict.fromkeys(v.strip() for v in vals if v and v.strip()))
        if field == "regions":
            # Limiting a rule to "Global" is the same as not limiting it.
            vals = [v for v in vals if v != GLOBAL_REGION]
        bad = [v for v in vals if v not in allowed.get(field, [])]
        if bad:
            raise HTTPException(status_code=422, detail=f"Unknown {field.replace('_', ' ')}: {', '.join(bad)}")
        cleaned[field] = vals
    return cleaned


def _clean_applies_to(values: Optional[List[str]]) -> Optional[List[str]]:
    """Lowercase and de-duplicate a rule's "applies to", rejecting anything the check can't match."""
    if values is None:
        return None
    vals = list(dict.fromkeys(v.strip().lower() for v in values if v and v.strip()))
    bad = [v for v in vals if v not in CONTENT_PARTS]
    if bad:
        raise HTTPException(
            status_code=422,
            detail=f"Unknown applies to: {', '.join(bad)}. Use any of: {', '.join(CONTENT_PARTS)}",
        )
    return vals


def _duplicate_number(rule_number: str) -> HTTPException:
    return HTTPException(
        status_code=409,
        detail=f'Rule number "{rule_number}" is already used by another rule (including removed baseline rules). Pick a different number.',
    )


async def list_compliance_rules(
    category: Optional[str] = None,
    severity: Optional[str] = None,
    traffic_light: Optional[str] = None,
    is_active: Optional[bool] = None,
    search: Optional[str] = None,
    rule_set: Optional[str] = None,
) -> List[ComplianceRule]:
    """Retrieve compliance rules with multi-criteria filtering. Removed rules are never returned.
    The unfiltered lists (all rules, or only active or inactive ones) are what every check and self-test loads, so
    they're cached until a rule changes (see RULES_CACHE_TTL); filtered searches always query."""
    cache_key = None
    if not (category or severity or traffic_light or search or rule_set):
        cache_key = f"rules:active={is_active}"
        cached = await rules_cache.get(cache_key)
        if cached is not None:
            return list(cached)
        generation = rules_cache.generation

    query = """
        SELECT *
        FROM compliance_rules
        WHERE deleted_at IS NULL
    """
    params: List[Any] = []
    idx = 1

    if category:
        query += f" AND category = ${idx}"
        params.append(category)
        idx += 1

    if severity:
        query += f" AND UPPER(severity) = UPPER(${idx})"
        params.append(severity)
        idx += 1

    if traffic_light:
        query += f" AND UPPER(traffic_light) = UPPER(${idx})"
        params.append(traffic_light)
        idx += 1

    if is_active is not None:
        query += f" AND is_active = ${idx}"
        params.append(is_active)
        idx += 1

    if rule_set:
        # Rules with no rule sets apply to every set, so they belong in every set's list.
        query += f" AND (${idx} = ANY(rule_sets) OR cardinality(rule_sets) = 0)"
        params.append(rule_set)
        idx += 1

    if search:
        query += (
            f" AND (rule_number ILIKE ${idx} OR title ILIKE ${idx} OR summary ILIKE ${idx}"
            f" OR array_to_string(prohibited_terms, ' ') ILIKE ${idx})"
        )
        params.append(f"%{search}%")
        idx += 1

    query += " ORDER BY sort_order ASC, rule_number ASC"
    rows = await fetch_all(query, *params)
    rules = [_row_to_rule(r) for r in rows]
    if cache_key:
        await rules_cache.set_if_unchanged(cache_key, rules, generation, RULES_CACHE_TTL)
    # A copy, so a caller changing its list can't change the cached one.
    return list(rules)


async def get_compliance_rule(rule_id: UUID) -> Optional[ComplianceRule]:
    """Fetch single compliance rule by ID."""
    row = await fetch_one("SELECT * FROM compliance_rules WHERE id = $1 AND deleted_at IS NULL", rule_id)
    return _row_to_rule(row) if row else None


async def create_compliance_rule(data: ComplianceRuleCreate, user_id: Optional[UUID] = None) -> ComplianceRule:
    """Add a new custom rule."""
    scope = await _clean_scope({f: getattr(data, f) for f in SCOPE_FIELDS})
    pool = get_pool()
    try:
        async with pool.acquire() as conn:
            row = await conn.fetchrow("""
                INSERT INTO compliance_rules (
                    rulebook_id, rule_number, category, title, summary,
                    severity, traffic_light, prohibited_terms, required_disclaimers,
                    allowed_examples, prohibited_examples, remediation_guidance,
                    applies_to, is_active, is_custom, sort_order, created_by, updated_by,
                    rule_sets, brands, audiences, regions, platforms, disclaimer_triggers, exception_terms
                )
                VALUES (
                    $1, $2, $3, $4, $5,
                    $6, $7, $8, $9,
                    $10, $11, $12,
                    $13, $14, true, $15, $16, $16,
                    $17, $18, $19, $20, $21, $22, $23
                )
                RETURNING *;
            """,
                data.rulebook_id,
                data.rule_number.strip(),
                data.category,
                data.title,
                data.summary,
                data.severity.upper(),
                data.traffic_light.upper(),
                data.prohibited_terms,
                data.required_disclaimers,
                data.allowed_examples,
                data.prohibited_examples,
                data.remediation_guidance,
                _clean_applies_to(data.applies_to),
                data.is_active,
                data.sort_order,
                user_id,
                scope["rule_sets"],
                scope["brands"],
                scope["audiences"],
                scope["regions"],
                scope["platforms"],
                data.disclaimer_triggers,
                data.exception_terms,
            )
    except asyncpg.UniqueViolationError:
        raise _duplicate_number(data.rule_number)

    await emit_cache_invalidation(scope="compliance_rules", key=str(row["id"]))
    reference_index_service.sync_soon()
    await log_audit_event(
        entity_type="compliance_rule",
        entity_id=str(row["id"]),
        action="CREATE_COMPLIANCE_RULE",
        user_id=user_id,
        changes={"rule_number": row["rule_number"], "title": data.title, "severity": data.severity, **scope},
    )
    return _row_to_rule(dict(row))


async def update_compliance_rule(rule_id: UUID, data: ComplianceRuleUpdate, user_id: Optional[UUID] = None) -> Optional[ComplianceRule]:
    """Update an existing rule. Fields left out (None) are not changed."""
    existing = await get_compliance_rule(rule_id)
    if not existing:
        return None

    fields: Dict[str, Any] = {
        "rule_number": data.rule_number.strip() if data.rule_number else None,
        "category": data.category,
        "title": data.title,
        "summary": data.summary,
        "severity": data.severity.upper() if data.severity else None,
        "traffic_light": data.traffic_light.upper() if data.traffic_light else None,
        "prohibited_terms": data.prohibited_terms,
        "required_disclaimers": data.required_disclaimers,
        "disclaimer_triggers": data.disclaimer_triggers,
        "exception_terms": data.exception_terms,
        "allowed_examples": data.allowed_examples,
        "prohibited_examples": data.prohibited_examples,
        "remediation_guidance": data.remediation_guidance,
        "applies_to": _clean_applies_to(data.applies_to),
        "is_active": data.is_active,
        "sort_order": data.sort_order,
        **await _clean_scope({f: getattr(data, f) for f in SCOPE_FIELDS}),
    }
    # Only record what actually changes, so the audit trail shows a real before/after.
    changed = {col: val for col, val in fields.items() if val is not None and val != getattr(existing, col)}
    if not changed:
        return existing

    updates = [f"{col} = ${i}" for i, col in enumerate(changed, start=1)]
    params: List[Any] = list(changed.values())
    updates.append("updated_at = NOW()")
    if user_id:
        params.append(user_id)
        updates.append(f"updated_by = ${len(params)}")
    params.append(rule_id)

    query = f"""
        UPDATE compliance_rules
        SET {', '.join(updates)}
        WHERE id = ${len(params)} AND deleted_at IS NULL
        RETURNING *;
    """

    pool = get_pool()
    try:
        async with pool.acquire() as conn:
            row = await conn.fetchrow(query, *params)
    except asyncpg.UniqueViolationError:
        raise _duplicate_number(changed.get("rule_number", ""))

    if not row:
        return None

    await emit_cache_invalidation(scope="compliance_rules", key=str(rule_id))
    reference_index_service.sync_soon()
    await log_audit_event(
        entity_type="compliance_rule",
        entity_id=str(rule_id),
        action="UPDATE_COMPLIANCE_RULE",
        user_id=user_id,
        changes={
            "rule_number": row["rule_number"],
            "before": {col: getattr(existing, col) for col in changed},
            "after": changed,
        },
    )
    return _row_to_rule(dict(row))


async def delete_compliance_rule(rule_id: UUID, user_id: Optional[UUID] = None) -> bool:
    """Remove a rule from screening. The row is kept (soft delete) so the audit trail and past
    check results still resolve, and so startup seeding doesn't bring removed baseline rules back."""
    existing = await get_compliance_rule(rule_id)
    if not existing:
        return False

    pool = get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            """
            UPDATE compliance_rules
            SET deleted_at = NOW(), is_active = false, updated_at = NOW(), updated_by = COALESCE($2, updated_by)
            WHERE id = $1
            """,
            rule_id,
            user_id,
        )

    await emit_cache_invalidation(scope="compliance_rules", key=str(rule_id))
    reference_index_service.sync_soon()
    await log_audit_event(
        entity_type="compliance_rule",
        entity_id=str(rule_id),
        action="DELETE_COMPLIANCE_RULE",
        user_id=user_id,
        changes={"rule_number": existing.rule_number, "title": existing.title},
    )
    return True


def _normalize(text: str) -> str:
    """Lowercase and collapse punctuation to spaces, so "combat-proven" matches "combat proven"."""
    return " ".join(re.sub(r"[\W_]+", " ", text.lower()).split())


@lru_cache(maxsize=2048)
def _term_pattern(term: str) -> Optional["re.Pattern[str]"]:
    """A trigger phrase as a regex over normalized text. "*" stands for up to three words, so
    "endorsed by * department of defense" also catches "endorsed by the U.S. Department of Defense"."""
    parts = [_normalize(p) for p in term.split("*")]
    if not any(parts):
        return None
    # Each gap is "up to three words, then a space", so "a * b" matches "a b" and "a x y b".
    gap = r"(?: \S+){0,3} "
    body = gap.join(re.escape(p) for p in parts)
    return re.compile(rf"(?<!\S){body}(?!\S)")


def _matches(term: str, padded_text: str) -> bool:
    if "*" not in term:
        t = _normalize(term)
        return bool(t) and f" {t} " in padded_text
    pattern = _term_pattern(term)
    return bool(pattern and pattern.search(padded_text))


def _spans(term: str, padded_text: str) -> List[Tuple[int, int]]:
    """Where a phrase occurs in normalized, space-padded text."""
    if "*" not in term:
        t = _normalize(term)
        if not t:
            return []
        needle, spans = f" {t} ", []
        i = padded_text.find(needle)
        while i != -1:
            spans.append((i + 1, i + 1 + len(t)))
            i = padded_text.find(needle, i + 1)
        return spans
    pattern = _term_pattern(term)
    return [m.span() for m in pattern.finditer(padded_text)] if pattern else []


def _hit(term: str, padded_text: str, exceptions: List[str]) -> bool:
    """A phrase counts unless every place it occurs sits inside one of the rule's exceptions, so "not combat
    proven" doesn't trip "combat proven" but "not just talk: combat proven" still does."""
    spans = _spans(term, padded_text)
    if not spans or not exceptions:
        return bool(spans)
    excused = [e for x in exceptions for e in _spans(x, padded_text)]
    return any(not any(es <= a and b <= ee for es, ee in excused) for a, b in spans)


def _hashtag_hit(term: str, hashtag_keys: Sequence[str], exceptions: List[str]) -> bool:
    """A multi-word phrase run together in a hashtag ("#combatproven" for "combat proven"), unless one of the rule's
    exceptions is run together around it too ("#notcombatproven"). One-word phrases need no help: a hashtag that is
    just that word is already matched in the text. CamelCase hashtags ("#CombatProven") are split into words before
    matching (see post_signals.hashtag_phrases), so this is for tags written in one case."""
    if "*" in term or not hashtag_keys:
        return False
    norm = _normalize(term)
    if " " not in norm:
        return False
    key = norm.replace(" ", "")
    excused = [k for k in (_normalize(e).replace(" ", "") for e in exceptions) if key in k]
    return any(key in tag and not any(e in tag for e in excused) for tag in hashtag_keys)


_SMALL_WORDS = {"the", "and", "for", "not", "or", "of", "a", "an", "to", "in", "on", "by", "is", "it"}


# How spread out a near-match disclaimer may be: its words must sit within this many times its own length.
DISCLAIMER_SPREAD = 1.5


def _disclaimer_present(disclaimer: str, padded_text: str) -> bool:
    """A disclaimer counts as present if it's there word for word, or nearly so: wording drifts a little
    between platforms ("DoD" vs "DOD", a dropped "the"), so 85% of its meaningful words will do, as long as they're
    together, within 1.5x the disclaimer's length. Its words scattered across the post are not the disclaimer."""
    norm = _normalize(disclaimer)
    if not norm:
        return True
    if f" {norm} " in padded_text:
        return True
    words = [w for w in norm.split() if w not in _SMALL_WORDS]
    if not words:
        return False
    wanted = set(words)
    window = max(len(norm.split()), int(len(norm.split()) * DISCLAIMER_SPREAD + 0.5))
    tokens = padded_text.split()
    # A matching stretch starts on one of the disclaimer's words, so only those starts are tried.
    for start, token in enumerate(tokens):
        if token not in wanted:
            continue
        near = set(tokens[start:start + window])
        if sum(1 for w in words if w in near) / len(words) >= 0.85:
            return True
    return False


def flag_segments(segments: List[Dict[str, Any]], violations: List[ComplianceViolation]) -> List[Dict[str, Any]]:
    """Which transcript segments contain flagged wording, using the same matcher as the check itself, so the
    reviewer's timeline always agrees with the score."""
    flags: List[Dict[str, Any]] = []
    for seg in segments:
        padded = f" {_normalize(str(seg.get('text', '')))} "
        hits = [v for v in violations if any(_hit(t, padded, v.exception_terms) for t in v.flagged_terms)]
        light = "GREEN"
        if hits:
            red = any(v.traffic_light == "RED" or v.severity.upper() == "CRITICAL" for v in hits)
            light = "RED" if red else "YELLOW"
        flags.append({
            "start_sec": seg.get("start_sec"),
            "end_sec": seg.get("end_sec"),
            "traffic_light": light,
            "rule_numbers": [v.rule_number for v in hits],
        })
    return flags


_VIDEO_WORDS = {"video"}
_IMAGE_WORDS = {"image", "imagery", "carousel"}


def parts_for(content_type: Optional[str], *, has_video: bool = False, has_images: bool = False,
              is_ceo: bool = False) -> List[str]:
    """What content is made of, as the parts a rule's "applies to" names. content_type is a submission type
    ("VIDEO", "CAROUSEL"...) or a screener type ("video_caption", "imagery_concept"), optionally with a platform
    ("VIDEO_FACEBOOK"). Its text ("caption") is always part of it. An ad is video or imagery by what's attached, and
    could be either when nothing is. Nothing known at all returns [], so no rule is left out by "applies to"."""
    words = set(re.split(r"[^a-z]+", (content_type or "").lower())) - {""}
    if not (words or has_video or has_images or is_ceo):
        return []
    parts = {"caption"}
    if has_video or words & _VIDEO_WORDS:
        parts.add("video")
    if has_images or words & _IMAGE_WORDS:
        parts.add("imagery")
    if "ad" in words and not (has_video or has_images):
        parts |= {"video", "imagery"}
    if is_ceo or "ceo" in words:
        parts.add("ceo_post")
    return sorted(parts)


# What a post says can widen who reads it, whatever audience and rule sets it was given (Linda: a "general" post with
# $ZENA in it still reaches investors). Each entry: the rule set its wording pulls in, the audience it implies, and
# the wording that does it. Phrases are matched like rule phrases (case and punctuation ignored); hashtags exactly.
INVESTOR_PHRASES = (
    "nasdaq zena", "zena stock", "zenatech stock", "our stock", "share price", "stock price",
    "shareholder", "shareholders", "stockholder", "stockholders", "investor", "investors", "earnings",
    "quarterly results", "annual results", "market cap", "investor relations",
)
INVESTOR_HASHTAGS = ("nasdaq", "zena", "zenastock", "investing", "investors", "stocks", "stockmarket", "smallcaps",
                     "pennystocks")
DEFENSE_PHRASES = (
    "dod", "department of defense", "department of war", "pentagon", "program manager", "program managers",
    "warfighter", "warfighters", "defense procurement", "us army", "u s army", "us navy", "u s navy", "us air force",
    "u s air force", "marine corps", "space force", "national guard", "military",
)
DEFENSE_HASHTAGS = ("dod", "defensetech", "defenseinnovation", "military", "nationalsecurity", "warfighter")


@dataclass(frozen=True)
class ImpliedBy:
    rule_set: str
    audience: str
    phrases: Tuple[str, ...]
    hashtags: Tuple[str, ...]
    # Whether a cashtag ($ZENA) counts (investors) and whether a tagged government or military account does (defense).
    cashtags: bool = False
    government_tags: bool = False


# Who an implied audience is, and the sentiment standard it's held to, in the reviewer's words.
_READERS = {INVESTOR_AUDIENCE: ("investors", "investor"), DEFENSE_AUDIENCE: ("defense buyers", "defense")}

IMPLIED_BY = (
    ImpliedBy(DISCLOSURE_RULE_SET, INVESTOR_AUDIENCE, INVESTOR_PHRASES, INVESTOR_HASHTAGS, cashtags=True),
    ImpliedBy(DEFENSE_RULE_SET, DEFENSE_AUDIENCE, DEFENSE_PHRASES, DEFENSE_HASHTAGS, government_tags=True),
)


def _wording_for(implied: ImpliedBy, padded_text: str, signals: post_signals.PostSignals) -> List[str]:
    """The wording in the post that brings in this rule set, as written where possible, at most 5."""
    found: List[str] = []
    if implied.cashtags:
        found += signals.cashtags
    if implied.government_tags:
        found += [g for g in signals.government_references if g.startswith("@")]
    keys = set(implied.hashtags)
    tags = [h for h in signals.hashtags if h.lstrip("#").lower().replace("_", "") in keys]
    found += tags
    # "#DoD" also reads as the word "dod"; report it once, as the hashtag.
    tag_keys = {h.lstrip("#").lower().replace("_", "") for h in tags}
    found += [p for p in implied.phrases if p.replace(" ", "") not in tag_keys and _hit(p, padded_text, [])]
    return list(dict.fromkeys(found))[:5]


def implied_by_wording(text: str, signals: Optional[post_signals.PostSignals] = None) -> Dict[str, List[str]]:
    """{rule set: the wording that brings it in} for every rule set the post's wording implies, whatever it was
    given. E.g. {"Public Company Disclosure": ["$ZENA", "shareholders"]}."""
    signals = signals or post_signals.extract(text)
    padded = f" {_normalize(text)} "
    out: Dict[str, List[str]] = {}
    for implied in IMPLIED_BY:
        wording = _wording_for(implied, padded, signals)
        if wording:
            out[implied.rule_set] = wording
    return out


def implied_audiences(audience: Optional[str], signals: post_signals.PostSignals, text: str = "") -> List[str]:
    """Audiences a post reaches whatever it targets: investor wording ($ZENA, "shareholders") means investors read it,
    defense wording ("Pentagon", "program managers") means defense buyers do. Without the text, only cashtags count."""
    wording = implied_by_wording(text, signals) if text else (
        {DISCLOSURE_RULE_SET: signals.cashtags} if signals.cashtags else {}
    )
    return [i.audience for i in IMPLIED_BY if i.rule_set in wording and i.audience != audience]


@dataclass(frozen=True)
class CheckScope:
    # The target audience plus any the post reaches anyway. Empty when no audience is set: every audience's rules run.
    audiences: List[str]
    # Added because of what the post says (see implied_audiences).
    implied_audiences: List[str]
    # What the content is made of (see parts_for). Empty when not known: every rule's "applies to" matches.
    content_parts: List[str]
    # The rule sets checked: the chosen ones plus any the wording brings in. Empty when none are chosen: every rule
    # set's rules run.
    rule_sets: List[str]
    # Rule sets added because of what the post says, with the wording that did it.
    added_rule_sets: Dict[str, List[str]]
    # Every rule set the wording implies (added or not), with the wording: for telling the reviewer why.
    wording: Dict[str, List[str]]


def check_scope(req: ComplianceCheckRequest, signals: Optional[post_signals.PostSignals] = None) -> CheckScope:
    """Who the content reaches and what it's made of, for picking the rules that apply to it."""
    signals = signals or post_signals.extract(req.text)
    wording = implied_by_wording(req.text, signals)
    implied = [i.audience for i in IMPLIED_BY if i.rule_set in wording and i.audience != req.audience]
    # With no audience set every audience-limited rule already runs; adding one would narrow that.
    audiences = [req.audience, *implied] if req.audience else []
    # The same for rule sets: with none chosen every rule already runs, so nothing is added.
    added = {rs: w for rs, w in wording.items() if req.rule_sets and rs not in req.rule_sets}
    rule_sets = [*req.rule_sets, *added] if req.rule_sets else []
    if req.content_parts:
        parts = sorted({p.strip().lower() for p in req.content_parts if p and p.strip()})
    else:
        parts = parts_for(req.content_type, is_ceo=req.brand_code == CEO_BRAND or CEO_RULE_SET in req.rule_sets)
    return CheckScope(audiences=audiences, implied_audiences=implied, content_parts=parts, rule_sets=rule_sets,
                      added_rule_sets=added, wording=wording)


def _rule_applies(rule: ComplianceRule, req: ComplianceCheckRequest, scope: Optional[CheckScope] = None) -> bool:
    """A rule applies unless it's limited to values the content doesn't have. Content that doesn't
    say (e.g. no audience set yet) is checked against every rule for that dimension."""
    scope = scope or check_scope(req)

    def overlaps(rule_values: List[str], content_values: List[str]) -> bool:
        return not rule_values or not content_values or bool(set(rule_values) & set(content_values))

    region = [] if not req.region or req.region == GLOBAL_REGION else [req.region]
    # Values the check can't match (from before "applies to" was validated) don't limit the rule.
    rule_parts = [p for p in (v.strip().lower() for v in rule.applies_to or []) if p in CONTENT_PARTS]
    return (
        overlaps(rule.rule_sets, scope.rule_sets)
        and overlaps(rule.brands, [req.brand_code] if req.brand_code else [])
        and overlaps(rule.audiences, scope.audiences)
        and overlaps(rule.regions, region)
        and overlaps(rule.platforms, req.platforms)
        and overlaps(rule_parts, scope.content_parts)
    )


async def applicable_rules(req: ComplianceCheckRequest) -> List[ComplianceRule]:
    """Active rules whose scope matches the content (also used to pick sentiment-analysis guidelines). Pass the
    post's text to have a cashtag in it bring in the investor rules (see implied_audiences)."""
    scope = check_scope(req)
    return [r for r in await list_compliance_rules(is_active=True) if _rule_applies(r, req, scope)]


# Points off per finding, and the most a post with a critical finding can score.
PENALTY_CRITICAL = 35
PENALTY_HIGH = 20
PENALTY_OTHER = 10
PENALTY_MISSING_DISCLAIMER = 20
CRITICAL_SCORE_CAP = 40


def _quoted(items: Sequence[str]) -> str:
    """'"a", "b"'. A helper rather than inline, since Python before 3.12 allows no backslash inside an f-string's {}."""
    return ", ".join(f'"{i}"' for i in items)


def _evaluate_rule(rule: ComplianceRule, padded_text: str,
                   hashtag_keys: Sequence[str] = ()) -> Optional[ComplianceViolation]:
    """One rule against normalized, padded text: its prohibited phrases (minus exceptions) and its required
    disclaimer. Used by the check and by the rule self-tests, so both always agree. hashtag_keys are the post's
    hashtags as one lowercase word each, so a phrase run together in a hashtag still counts."""
    exceptions = list(rule.exception_terms or [])

    def found(term: str) -> bool:
        return _hit(term, padded_text, exceptions) or _hashtag_hit(term, hashtag_keys, exceptions)

    flagged = [t for t in rule.prohibited_terms if found(t)]

    # Required disclaimers: needed when one of the rule's trigger phrases appears (or always, if it has
    # none). Any one of the listed disclaimers satisfies the rule.
    missing: List[str] = []
    triggered_by: List[str] = []
    if rule.required_disclaimers:
        triggered_by = [t for t in rule.disclaimer_triggers if found(t)]
        needed = not rule.disclaimer_triggers or bool(triggered_by) or bool(flagged)
        if needed and not any(_disclaimer_present(d, padded_text) for d in rule.required_disclaimers):
            missing = list(rule.required_disclaimers)

    if flagged:
        severity = rule.severity.upper()
        if severity == "CRITICAL" or rule.traffic_light == "RED":
            penalty, light = PENALTY_CRITICAL, "RED"
        elif severity == "HIGH" or rule.traffic_light == "YELLOW":
            penalty, light = PENALTY_HIGH, "YELLOW"
        else:
            # A finding is never green, even from a green (low-risk) rule: it still needs a reviewer's look.
            penalty, light = PENALTY_OTHER, "YELLOW"
        return ComplianceViolation(
            rule_id=rule.id,
            rule_number=rule.rule_number,
            title=rule.title,
            category=rule.category,
            severity=rule.severity,
            traffic_light=light,
            flagged_terms=flagged,
            explanation=f"Text triggered prohibited phrase(s): {_quoted(flagged)}. {rule.summary}"
            + (f" It also needs the disclaimer: \"{missing[0]}\"." if missing else ""),
            remediation_guidance=rule.remediation_guidance,
            missing_disclaimers=missing,
            penalty=penalty,
            exception_terms=exceptions,
        )
    if missing:
        # Adding the disclaimer fixes it, so it needs review rather than stopping publication.
        what = f"mentions {_quoted(triggered_by)}" if triggered_by else "is covered by this rule"
        return ComplianceViolation(
            rule_id=rule.id,
            rule_number=rule.rule_number,
            title=rule.title,
            category=rule.category,
            severity="HIGH",
            traffic_light="YELLOW",
            flagged_terms=triggered_by,
            explanation=f"Required disclaimer missing: the content {what}, so it must carry: \"{missing[0]}\""
            + (f" (or one of {len(missing) - 1} other approved wordings)." if len(missing) > 1 else "."),
            remediation_guidance=rule.remediation_guidance,
            kind="MISSING_DISCLAIMER",
            missing_disclaimers=missing,
            penalty=PENALTY_MISSING_DISCLAIMER,
            exception_terms=exceptions,
        )
    return None


async def check_compliance(req: ComplianceCheckRequest) -> ComplianceCheckResponse:
    """Screen text against the active rules that apply to this content's rule sets, brand,
    audience, region and platforms. Each finding takes its penalty off 100; any critical finding blocks
    publication and caps the score, so a post that can't go out never looks like a pass."""
    # Cashtags, hashtags, mentions and links, read before normalizing strips their symbols, and before the rules are
    # picked: a cashtag brings in the investor rules. CamelCase hashtags are added as words ("#CombatProven" ->
    # "combat proven") so phrase rules see them.
    signals = post_signals.extract(req.text)
    scope = check_scope(req, signals)
    rules = await applicable_rules(req)
    padded_text = f" {_normalize(chr(10).join([req.text, *signals.hashtag_phrases]))} "
    hashtag_keys = signals.hashtag_keys
    violations: List[ComplianceViolation] = []
    # Two rules can ask for the same disclaimer (e.g. Rules 4 and 13); a missing one is reported once.
    reported_disclaimers: set = set()
    for rule in rules:
        v = _evaluate_rule(rule, padded_text, hashtag_keys)
        if not v:
            continue
        if v.missing_disclaimers:
            key = _normalize(v.missing_disclaimers[0])
            if v.kind == "MISSING_DISCLAIMER" and key in reported_disclaimers:
                continue
            reported_disclaimers.add(key)
        violations.append(v)

    # Built-in claim checks catch what exact phrases miss (money figures, forward-looking results, unnamed
    # customers...). A forward-looking claim the rulebook already reported as missing its safe harbor counts once.
    safe_harbor_reported = any(
        v.kind == "MISSING_DISCLAIMER" and any("forward looking" in _normalize(d) for d in v.missing_disclaimers)
        for v in violations
    )
    violations.extend(
        c for c in claim_checks.find_claims(req.text)
        if not (safe_harbor_reported and c.rule_id == claim_checks.FORWARD_LOOKING_ID)
    )
    # Tagged government or military accounts and linked .gov / .mil sites (see post_signals). When the rulebook
    # already reported the missing "no endorsement" disclaimer (Rules 4 and 13), the fix is the same, so it counts once.
    endorsement_reported = any(
        v.kind == "MISSING_DISCLAIMER" and any("dod endorsement" in _normalize(d) for d in v.missing_disclaimers)
        for v in violations
    )
    violations.extend(
        ComplianceViolation(
            rule_id=f.rule_id,
            rule_number=f.rule_number,
            title=f.title,
            category=f.category,
            severity=f.severity,
            traffic_light=f.traffic_light,
            flagged_terms=f.flagged_terms,
            explanation=f.explanation,
            remediation_guidance=f.remediation,
            penalty=f.penalty,
        )
        for f in post_signals.findings(signals)
        if not (endorsement_reported and f.rule_id == post_signals.GOVERNMENT_REFERENCE_ID)
    )

    _count_overlaps_once(violations, padded_text)
    blocked = any(v.traffic_light == "RED" for v in violations)
    # GREEN means nothing was found. Any finding makes it at least YELLOW, so the light, the score and "compliant"
    # always agree.
    overall_traffic_light = "RED" if blocked else "YELLOW" if violations else "GREEN"
    raw_score = max(0, 100 - sum(v.penalty for v in violations))
    score = min(raw_score, CRITICAL_SCORE_CAP) if blocked else raw_score

    recommendations: List[str] = []
    if blocked:
        recommendations.append("STOP PUBLICATION: Critical red-line compliance violation detected. Legal/Compliance clearance required.")
    elif overall_traffic_light == "YELLOW":
        recommendations.append(
            f"REVIEW REQUIRED: {len(violations)} finding{'s' if len(violations) != 1 else ''} to fix or dismiss with a "
            "reason before publishing. Second-party Comms/IR review required."
        )
    elif not rules:
        recommendations.append("No active rules apply to the selected rule sets, brand, audience, region and platforms yet. Add them in the Compliance Rulebook.")
    else:
        recommendations.append(f"No issues found against the {len(rules)} rule(s) that apply to this content.")

    # Rules with nothing to look for in text (no phrases, no disclaimers) still apply; say so instead of letting a
    # clean score suggest they passed.
    manual = [
        ManualReviewRule(rule_id=r.id, rule_number=r.rule_number, title=r.title, severity=r.severity)
        for r in rules
        if not r.prohibited_terms and not r.required_disclaimers
    ]
    notes = signals.notes()
    # Why the check reached further than the post's settings: first in the notes, investors before defense.
    for implied in reversed(IMPLIED_BY):
        added = implied.rule_set in scope.added_rule_sets
        reaches = implied.audience in scope.implied_audiences
        if not (added or reaches):
            continue
        because = _quoted(scope.wording.get(implied.rule_set, []))
        readers, standard = _READERS[implied.audience]
        if added:
            note = f"{implied.rule_set} rules added because of {because}"
            note += f": {readers} will read this post." if reaches else "."
        elif not req.rule_sets:
            note = f"{because} means {readers} will read this post. No rule sets are chosen, so every rule set was checked."
        else:
            note = f"{because} means {readers} will read this post; the {implied.rule_set} rules were already chosen."
        if reaches:
            note += f" The sentiment check holds it to the {standard} standard."
        notes.insert(0, note)
    return ComplianceCheckResponse(
        audiences_checked=scope.audiences,
        content_parts=scope.content_parts,
        added_rule_sets=[AddedRuleSet(rule_set=rs, because=w) for rs, w in scope.added_rule_sets.items()],
        manual_review_rules=manual,
        is_compliant=(len(violations) == 0),
        traffic_light=overall_traffic_light,
        score=score,
        raw_score=raw_score,
        blocked=blocked,
        score_cap=CRITICAL_SCORE_CAP if blocked and raw_score > CRITICAL_SCORE_CAP else None,
        violations_count=len(violations),
        violations=violations,
        recommendations=recommendations,
        rules_checked=len(rules),
        rulebook_version=await rulebook_version(),
        signals=CheckSignals(
            cashtags=signals.cashtags,
            hashtags=signals.hashtags,
            mentions=signals.mentions,
            links=signals.links,
            government_references=signals.government_references,
            implied_audiences=scope.implied_audiences,
            notes=notes,
        ),
    )


def flagged_tags(tags: Sequence[str], rules: List[ComplianceRule]) -> Dict[str, str]:
    """Which of these hashtags the rules would flag on their own, as {tag: rule number}. Used to keep the hashtag
    library and caption suggestions from offering a tag the check would then flag."""
    out: Dict[str, str] = {}
    for tag in tags:
        signals = post_signals.extract(tag)
        padded = f" {_normalize(chr(10).join([tag, *signals.hashtag_phrases]))} "
        for rule in rules:
            v = _evaluate_rule(rule, padded, signals.hashtag_keys)
            # A tag only "needs a disclaimer" because the rule wants one on everything; that's the post's job.
            if v and v.kind == "PROHIBITED_TERM":
                out[tag] = rule.rule_number
                break
    return out


def _count_overlaps_once(violations: List[ComplianceViolation], padded_text: str) -> None:
    """When two rules flag the very same words (e.g. one phrase in both a maturity rule and a vocabulary rule),
    the words cost points once, at the stricter rule's penalty. The other finding stays listed, at 0 points."""
    def spans(v: ComplianceViolation) -> List[Tuple[int, int]]:
        return [sp for t in v.flagged_terms for sp in _spans(t, padded_text)]

    counted: List[Tuple[Tuple[int, int], str]] = []
    for v in sorted((v for v in violations if v.kind == "PROHIBITED_TERM"), key=lambda v: -v.penalty):
        mine = spans(v)
        if not mine:
            continue
        cover = [rule for (a, b), rule in counted if any(x < b and a < y for x, y in mine)]
        if cover and all(any(x < b and a < y for (a, b), _ in counted) for x, y in mine):
            v.counted_with = cover[0]
            v.uncounted_penalty = v.penalty
            v.penalty = 0
            continue
        counted.extend((sp, v.rule_number) for sp in mine)


async def rulebook_version() -> Optional[str]:
    """When any rule last changed (added, edited or removed), plus the built-in claim and signal checks' versions, so
    a check made before any of them changed counts as outdated. Cached with the rule lists, and dropped with them."""
    cached = await rules_cache.get("version")
    if cached is not None:
        return cached
    generation = rules_cache.generation
    row = await fetch_one("SELECT max(greatest(updated_at, coalesce(deleted_at, updated_at))) AS v FROM compliance_rules")
    rules = row["v"].isoformat() if row and row["v"] else "none"
    version = f"{rules}|{claim_checks.VERSION}|{post_signals.VERSION}|{SCOPE_VERSION}"
    await rules_cache.set_if_unchanged("version", version, generation, RULES_CACHE_TTL)
    return version


# ---------------------------------------------------------------------------
# Rule self-tests: every rule checked against its own examples
# ---------------------------------------------------------------------------

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


def self_test(rule: ComplianceRule) -> RuleSelfTest:
    """Run a rule against its quoted "not acceptable" examples (it should flag them) and its quoted "acceptable"
    ones (it shouldn't). Rules with no phrases, or no quoted wording to test, are marked untestable."""
    has_checks = bool(rule.prohibited_terms or rule.disclaimer_triggers or rule.required_disclaimers)
    # "Acceptable" text is usually a description of what to do, so only its quoted wording is tested.
    cases = [("FLAG", ex) for ex in _examples(rule.prohibited_examples, unquoted=True)]
    cases += [("CLEAR", ex) for ex in _examples(rule.allowed_examples, unquoted=False)]
    testable = has_checks and bool(cases)
    results: List[RuleExampleResult] = []
    if testable:
        for expected, (example, quoted) in cases:
            v = _evaluate_rule(rule, f" {_normalize(example)} ")
            flagged = v is not None
            results.append(RuleExampleResult(
                example=example[:300],
                expected=expected,
                flagged=flagged,
                matched_terms=list(v.flagged_terms) if v else [],
                ok=flagged == (expected == "FLAG"),
                quoted=quoted,
            ))
    return RuleSelfTest(
        rule_id=rule.id,
        rule_number=rule.rule_number,
        title=rule.title,
        testable=testable,
        passed=all(r.ok for r in results),
        results=results,
    )


async def self_test_all() -> List[RuleSelfTest]:
    return [self_test(r) for r in await list_compliance_rules()]


async def get_compliance_stats() -> Dict[str, Any]:
    """Retrieve aggregate compliance metrics. Only active rules count toward the risk tiers."""
    rules = await list_compliance_rules()
    active_rules = [r for r in rules if r.is_active]

    categories: Dict[str, int] = {}
    for r in rules:
        categories[r.category] = categories.get(r.category, 0) + 1

    rule_sets = {name: 0 for name in RULE_SETS}
    for r in active_rules:
        for name in r.rule_sets or RULE_SETS:
            rule_sets[name] = rule_sets.get(name, 0) + 1

    return {
        "total_rules": len(rules),
        "active_rules": len(active_rules),
        "critical_red_rules": sum(1 for r in active_rules if r.traffic_light == "RED"),
        "yellow_review_rules": sum(1 for r in active_rules if r.traffic_light == "YELLOW"),
        "green_standard_rules": sum(1 for r in active_rules if r.traffic_light == "GREEN"),
        "categories_count": len(categories),
        "categories": categories,
        "rule_sets": rule_sets,
    }
