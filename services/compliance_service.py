"""Compliance Rules & Governance Service
Rule CRUD and rulebook screening of captions, scripts and transcripts.
"""

import logging
import re
from typing import Any, Dict, List, Optional
from uuid import UUID

import asyncpg
from fastapi import HTTPException

from models.compliance_model import (
    ComplianceCheckRequest,
    ComplianceCheckResponse,
    ComplianceRule,
    ComplianceRuleCreate,
    ComplianceRuleUpdate,
    ComplianceViolation,
)
from models.submission_model import AUDIENCES, PLATFORMS, REGIONS, RULE_SETS
from postgresql_db.database import fetch_all, fetch_one, get_pool
from services.audit_service import log_audit_event
from services.cache_service import emit_cache_invalidation

logger = logging.getLogger(__name__)

SCOPE_FIELDS = ("rule_sets", "brands", "audiences", "regions", "platforms")
# Region a submission uses for "posted everywhere"; it matches every region-scoped rule.
GLOBAL_REGION = "Global"


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
        allowed["brands"] = [r["code"] for r in await fetch_all("SELECT code FROM brands")]

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
    """Retrieve compliance rules with multi-criteria filtering. Removed rules are never returned."""
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
    return [_row_to_rule(r) for r in rows]


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
                    rule_sets, brands, audiences, regions, platforms
                )
                VALUES (
                    $1, $2, $3, $4, $5,
                    $6, $7, $8, $9,
                    $10, $11, $12,
                    $13, $14, true, $15, $16, $16,
                    $17, $18, $19, $20, $21
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
                data.applies_to,
                data.is_active,
                data.sort_order,
                user_id,
                scope["rule_sets"],
                scope["brands"],
                scope["audiences"],
                scope["regions"],
                scope["platforms"],
            )
    except asyncpg.UniqueViolationError:
        raise _duplicate_number(data.rule_number)

    await emit_cache_invalidation(scope="compliance_rules", key=str(row["id"]))
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
        "allowed_examples": data.allowed_examples,
        "prohibited_examples": data.prohibited_examples,
        "remediation_guidance": data.remediation_guidance,
        "applies_to": data.applies_to,
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


def _rule_applies(rule: ComplianceRule, req: ComplianceCheckRequest) -> bool:
    """A rule applies unless it's limited to values the content doesn't have. Content that doesn't
    say (e.g. no audience set yet) is checked against every rule for that dimension."""

    def overlaps(rule_values: List[str], content_values: List[str]) -> bool:
        return not rule_values or not content_values or bool(set(rule_values) & set(content_values))

    region = [] if not req.region or req.region == GLOBAL_REGION else [req.region]
    return (
        overlaps(rule.rule_sets, req.rule_sets)
        and overlaps(rule.brands, [req.brand_code] if req.brand_code else [])
        and overlaps(rule.audiences, [req.audience] if req.audience else [])
        and overlaps(rule.regions, region)
        and overlaps(rule.platforms, req.platforms)
    )


async def applicable_rules(req: ComplianceCheckRequest) -> List[ComplianceRule]:
    """Active rules whose scope matches the content (also used to pick sentiment-analysis guidelines)."""
    return [r for r in await list_compliance_rules(is_active=True) if _rule_applies(r, req)]


async def check_compliance(req: ComplianceCheckRequest) -> ComplianceCheckResponse:
    """Screen text against the active rules that apply to this content's rule sets, brand,
    audience, region and platforms."""
    rules = await applicable_rules(req)
    padded_text = f" {_normalize(req.text)} "
    violations: List[ComplianceViolation] = []
    overall_traffic_light = "GREEN"
    score = 100

    for rule in rules:
        flagged: List[str] = []
        for term in rule.prohibited_terms:
            t = _normalize(term)
            if t and f" {t} " in padded_text:
                flagged.append(term)

        if flagged:
            severity = rule.severity.upper()
            if severity == "CRITICAL" or rule.traffic_light == "RED":
                overall_traffic_light = "RED"
                penalty = 35
            elif severity == "HIGH" or rule.traffic_light == "YELLOW":
                if overall_traffic_light != "RED":
                    overall_traffic_light = "YELLOW"
                penalty = 20
            else:
                penalty = 10
            score = max(0, score - penalty)

            violations.append(
                ComplianceViolation(
                    rule_id=rule.id,
                    rule_number=rule.rule_number,
                    title=rule.title,
                    category=rule.category,
                    severity=rule.severity,
                    traffic_light=rule.traffic_light,
                    flagged_terms=flagged,
                    explanation=f"Text triggered prohibited phrase(s): {', '.join(f'\"{f}\"' for f in flagged)}. {rule.summary}",
                    remediation_guidance=rule.remediation_guidance,
                )
            )

    recommendations: List[str] = []
    if overall_traffic_light == "RED":
        recommendations.append("STOP PUBLICATION: Critical red-line compliance violation detected. Legal/Compliance clearance required.")
    elif overall_traffic_light == "YELLOW":
        recommendations.append("REVIEW REQUIRED: High-risk or developmental maturity claims detected. Second-party Comms/IR review required.")
    elif not rules:
        recommendations.append("No active rules apply to the selected rule sets, brand, audience, region and platforms yet. Add them in the Compliance Rulebook.")
    else:
        recommendations.append(f"No issues found against the {len(rules)} rule(s) that apply to this content.")

    return ComplianceCheckResponse(
        is_compliant=(len(violations) == 0),
        traffic_light=overall_traffic_light,
        score=score,
        violations_count=len(violations),
        violations=violations,
        recommendations=recommendations,
        rules_checked=len(rules),
    )


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
