"""Compliance Rules & Governance Service
Implements full CRUD ("build on it and remove") and intelligent AI text/claim/transcript screening.
"""

import logging
import re
from typing import Any, Dict, List, Optional
from uuid import UUID

from models.compliance_model import (
    ComplianceCheckRequest,
    ComplianceCheckResponse,
    ComplianceRule,
    ComplianceRuleCreate,
    ComplianceRuleUpdate,
    ComplianceViolation,
)
from postgresql_db.database import fetch_all, fetch_one, execute, get_pool
from services.audit_service import log_audit_event
from services.cache_service import emit_cache_invalidation

logger = logging.getLogger(__name__)


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
        is_active=row["is_active"],
        is_custom=row.get("is_custom", False),
        sort_order=row.get("sort_order", 0),
        created_at=row["created_at"],
        updated_at=row["updated_at"],
        created_by=row.get("created_by"),
        updated_by=row.get("updated_by"),
    )


async def list_compliance_rules(
    category: Optional[str] = None,
    severity: Optional[str] = None,
    traffic_light: Optional[str] = None,
    is_active: Optional[bool] = None,
    search: Optional[str] = None,
) -> List[ComplianceRule]:
    """Retrieve compliance rules with multi-criteria filtering."""
    query = """
        SELECT *
        FROM compliance_rules
        WHERE 1=1
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

    if search:
        query += f" AND (rule_number ILIKE ${idx} OR title ILIKE ${idx} OR summary ILIKE ${idx})"
        params.append(f"%{search}%")
        idx += 1

    query += " ORDER BY sort_order ASC, rule_number ASC"
    rows = await fetch_all(query, *params)
    return [_row_to_rule(r) for r in rows]


async def get_compliance_rule(rule_id: UUID) -> Optional[ComplianceRule]:
    """Fetch single compliance rule by ID."""
    row = await fetch_one("SELECT * FROM compliance_rules WHERE id = $1", rule_id)
    return _row_to_rule(row) if row else None


async def create_compliance_rule(data: ComplianceRuleCreate, user_id: Optional[UUID] = None) -> ComplianceRule:
    """Build on compliance rules: Add a new rule."""
    pool = get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow("""
            INSERT INTO compliance_rules (
                rulebook_id, rule_number, category, title, summary,
                severity, traffic_light, prohibited_terms, required_disclaimers,
                allowed_examples, prohibited_examples, remediation_guidance,
                applies_to, is_active, is_custom, sort_order, created_by, updated_by
            )
            VALUES (
                $1, $2, $3, $4, $5,
                $6, $7, $8, $9,
                $10, $11, $12,
                $13, $14, true, $15, $16, $16
            )
            RETURNING *;
        """,
            data.rulebook_id,
            data.rule_number,
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
        )

    await emit_cache_invalidation(scope="compliance_rules", key=str(row["id"]))
    await log_audit_event(
        actor_user_id=user_id,
        action="CREATE_COMPLIANCE_RULE",
        entity_type="compliance_rule",
        entity_id=str(row["id"]),
        new_value={"rule_number": data.rule_number, "title": data.title, "severity": data.severity},
    )
    return _row_to_rule(dict(row))


async def update_compliance_rule(rule_id: UUID, data: ComplianceRuleUpdate, user_id: Optional[UUID] = None) -> Optional[ComplianceRule]:
    """Update or refine an existing compliance rule."""
    existing = await get_compliance_rule(rule_id)
    if not existing:
        return None

    updates = []
    params: List[Any] = []
    idx = 1

    fields = {
        "rule_number": data.rule_number,
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
    }

    for col, val in fields.items():
        if val is not None:
            updates.append(f"{col} = ${idx}")
            params.append(val)
            idx += 1

    if not updates:
        return existing

    updates.append("updated_at = NOW()")
    if user_id:
        updates.append(f"updated_by = ${idx}")
        params.append(user_id)
        idx += 1

    params.append(rule_id)
    query = f"""
        UPDATE compliance_rules
        SET {', '.join(updates)}
        WHERE id = ${idx}
        RETURNING *;
    """

    pool = get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(query, *params)

    if not row:
        return None

    await emit_cache_invalidation(scope="compliance_rules", key=str(rule_id))
    await log_audit_event(
        actor_user_id=user_id,
        action="UPDATE_COMPLIANCE_RULE",
        entity_type="compliance_rule",
        entity_id=str(rule_id),
        old_value={"title": existing.title, "severity": existing.severity},
        new_value=fields,
    )
    return _row_to_rule(dict(row))


async def delete_compliance_rule(rule_id: UUID, user_id: Optional[UUID] = None) -> bool:
    """Remove a compliance rule ("and remove")."""
    existing = await get_compliance_rule(rule_id)
    if not existing:
        return False

    pool = get_pool()
    async with pool.acquire() as conn:
        await conn.execute("DELETE FROM compliance_rules WHERE id = $1", rule_id)

    await emit_cache_invalidation(scope="compliance_rules", key=str(rule_id))
    await log_audit_event(
        actor_user_id=user_id,
        action="DELETE_COMPLIANCE_RULE",
        entity_type="compliance_rule",
        entity_id=str(rule_id),
        old_value={"rule_number": existing.rule_number, "title": existing.title},
    )
    return True


async def check_compliance(req: ComplianceCheckRequest) -> ComplianceCheckResponse:
    """Analyze text/captions/transcripts against all active compliance rules."""
    rules = await list_compliance_rules(is_active=True)
    text_lower = req.text.lower()
    violations: List[ComplianceViolation] = []
    overall_traffic_light = "GREEN"
    score = 100

    for rule in rules:
        flagged: List[str] = []
        for term in rule.prohibited_terms:
            t = term.strip().lower()
            if not t:
                continue
            pattern = r'\b' + re.escape(t) + r'\b'
            if re.search(pattern, text_lower):
                flagged.append(term)

        if flagged:
            severity = rule.severity.upper()
            if severity == "CRITICAL" or rule.traffic_light == "RED":
                overall_traffic_light = "RED"
                score = max(0, score - 35)
            elif severity == "HIGH" or rule.traffic_light == "YELLOW":
                if overall_traffic_light != "RED":
                    overall_traffic_light = "YELLOW"
                score = max(10, score - 20)
            else:
                score = max(20, score - 10)

            violations.append(
                ComplianceViolation(
                    rule_id=rule.id,
                    rule_number=rule.rule_number,
                    title=rule.title,
                    category=rule.category,
                    severity=rule.severity,
                    traffic_light=rule.traffic_light,
                    flagged_terms=flagged,
                    explanation=f"Text triggered prohibited defense phrase(s): {', '.join(f'\"{f}\"' for f in flagged)}. {rule.summary}",
                    remediation_guidance=rule.remediation_guidance,
                )
            )

    recommendations: List[str] = []
    if overall_traffic_light == "RED":
        recommendations.append("STOP PUBLICATION: Critical Red-Line Defense Compliance violation detected. Legal/Compliance clearance required.")
    elif overall_traffic_light == "YELLOW":
        recommendations.append("REVIEW REQUIRED: High-risk or developmental maturity claims detected. Second-party Comms/IR review required.")
    else:
        recommendations.append("Compliant with ZenaTech & ZenaDrone Defense Thought Leadership & Imagery Rulebook.")

    return ComplianceCheckResponse(
        is_compliant=(len(violations) == 0),
        traffic_light=overall_traffic_light,
        score=score,
        violations_count=len(violations),
        violations=violations,
        recommendations=recommendations,
    )


async def get_compliance_stats() -> Dict[str, Any]:
    """Retrieve aggregate compliance metrics."""
    rules = await list_compliance_rules()
    total = len(rules)
    active = sum(1 for r in rules if r.is_active)
    critical = sum(1 for r in rules if r.is_active and r.traffic_light == "RED")
    review_req = sum(1 for r in rules if r.is_active and r.traffic_light == "YELLOW")
    standard = sum(1 for r in rules if r.is_active and r.traffic_light == "GREEN")

    categories = {}
    for r in rules:
        categories[r.category] = categories.get(r.category, 0) + 1

    return {
        "total_rules": total,
        "active_rules": active,
        "critical_red_rules": critical,
        "yellow_review_rules": review_req,
        "green_standard_rules": standard,
        "categories_count": len(categories),
        "categories": categories,
    }
