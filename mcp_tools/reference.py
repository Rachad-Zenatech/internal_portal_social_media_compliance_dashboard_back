import json
from typing import Optional

from services import reference_index_service, vector_store_service


async def search_reference_material_tool(query: str, kind: Optional[str] = None, brand: Optional[str] = None,
                                         limit: int = 5) -> str:
    """
    Search the compliance rulebook and the Messaging Context (brand voice, company facts, platform norms, audience
    profiles) by meaning, and return the passages that best answer the question.

    What it does:
        Embeds the question and runs a vector search over the reference material, so it finds the right passage
        even when the wording differs (e.g. "can we say the drone is combat proven?" finds the rule on product
        maturity claims).

    When to use:
        - Any question about what may or may not be posted, required disclaimers, or why something was flagged.
        - Questions about brand voice, approved company facts and figures, platform norms or audiences.

    Args:
        query: The question or topic, in plain words.
        kind: Optional filter: COMPLIANCE_RULE, BRAND_VOICE, FACT, PLATFORM_NORM or AUDIENCE_PROFILE.
        brand: Optional brand code, to only get material that applies to that brand (or to every brand).
        limit: How many passages to return (1-10).

    Returns:
        str: JSON list of passages (kind, title, text, score and, for rules, rule number, severity, rule sets).

    Side Effects / Confirmation:
        Read-only.
    """
    if not vector_store_service.is_enabled():
        return ("Reference search isn't configured on this server (no vector store). "
                "Use list_compliance_rules_tool to look rules up by keyword instead.")
    kinds = [kind.strip().upper()] if kind else None
    rows = await reference_index_service.search(query, kinds=kinds, brand=brand, limit=max(1, min(int(limit), 10)))
    if not rows:
        return "No reference material matched that question."
    return json.dumps(rows, default=str)


async def list_compliance_rules_tool(search: Optional[str] = None, rule_set: Optional[str] = None,
                                     severity: Optional[str] = None) -> str:
    """
    Look up active compliance rules by keyword, rule set or severity.

    What it does:
        Returns the matching rules from the rulebook with their number, title, severity, traffic light, summary,
        prohibited terms, required disclaimers and how to fix a violation.

    When to use:
        - "What does Rule 7 say?", "Which rules are CRITICAL?", "What rules apply to ZenaDrone Defense posts?"
        - When an exact keyword or rule number is known (use search_reference_material_tool for questions).

    Args:
        search: Optional text to match in the rule number, title, summary or prohibited terms.
        rule_set: Optional rule set name, e.g. "ZenaDrone Defense".
        severity: Optional CRITICAL, HIGH, MEDIUM or LOW.

    Returns:
        str: JSON list of rules (at most 25).

    Side Effects / Confirmation:
        Read-only.
    """
    from services.compliance_service import list_compliance_rules

    rules = await list_compliance_rules(severity=severity, is_active=True, search=search, rule_set=rule_set)
    if not rules:
        return "No active rules matched."
    return json.dumps([
        {
            "rule_number": r.rule_number, "title": r.title, "category": r.category, "severity": r.severity,
            "traffic_light": r.traffic_light, "summary": r.summary, "prohibited_terms": r.prohibited_terms,
            "required_disclaimers": r.required_disclaimers, "remediation_guidance": r.remediation_guidance,
            "rule_sets": r.rule_sets,
        }
        for r in rules[:25]
    ], default=str)
