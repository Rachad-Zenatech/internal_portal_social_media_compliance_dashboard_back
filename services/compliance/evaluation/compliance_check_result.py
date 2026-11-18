"""Recommendations, scope notes and response assembly for a completed compliance check."""
from typing import List
from models.compliance.compliance_model import AddedRuleSet, CheckSignals, ComplianceCheckResponse, ManualReviewRule


def review_recommendations(blocked, breaches, needs_review, needs_evidence, to_check, rules) -> List[str]:
    recommendations: List[str] = []
    if blocked:
        recommendations.append("STOP PUBLICATION: Critical red-line compliance violation detected. Legal/Compliance clearance required.")
    elif breaches:
        recommendations.append(
            f"REVIEW REQUIRED: {len(breaches)} finding{'s' if len(breaches) != 1 else ''} to fix or dismiss with a "
            "reason before publishing. Second-party Comms/IR review required."
        )
    if (needs_review or needs_evidence) and not blocked:
        parts = []
        if needs_review:
            parts.append(f"{needs_review} to check by a person (what the final visuals show, for example)")
        if needs_evidence:
            parts.append(f"{needs_evidence} claim{'s' if needs_evidence != 1 else ''} that need{'' if needs_evidence != 1 else 's'} a source")
        note = "Confirm them before publishing."
        if needs_evidence:
            note = ("A claim that needs a source costs a few points until a reviewer confirms the evidence is on file. "
                    "Confirm them before publishing.")
        if any(v.status == "NEEDS_REVIEW" for v in to_check):
            note += " A check the AI thinks is likely a breach costs points until it's fixed or dismissed."
        recommendations.append(
            ("No rule breaches found, but " if not breaches else "Also: ") + " and ".join(parts) + ". " + note
        )
    if not recommendations:
        if not rules:
            recommendations.append("No active rules apply to the selected rule sets, brand, audience, region and platforms yet. Add them in the Compliance Rulebook.")
        else:
            recommendations.append(f"No issues found against the {len(rules)} rule(s) that apply to this content.")

    return recommendations


def check_notes(req, scope, signals, implied_by, readers_by_audience) -> List[str]:
    notes = signals.notes()
    # Why the check reached further than the post's settings: first in the notes, investors before defense.
    for implied in reversed(implied_by):
        added = implied.rule_set in scope.added_rule_sets
        reaches = implied.audience in scope.implied_audiences
        if not (added or reaches):
            continue
        because = ", ".join(f'"{word}"' for word in scope.wording.get(implied.rule_set, []))
        readers, standard = readers_by_audience[implied.audience]
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
    return notes


def build_response(scope, rules, violations, breaches, unsupported, to_check, signals,
                   ai_unavailable, notes, source_checks, version) -> ComplianceCheckResponse:
    blocked = any(v.traffic_light == "RED" for v in breaches)
    needs_review = sum(1 for v in violations if v.status == "NEEDS_REVIEW")
    needs_evidence = sum(1 for v in violations if v.status == "EVIDENCE_REQUIRED")
    cleared = sum(1 for v in violations if v.status == "CLEARED")
    open_warnings = needs_review + needs_evidence
    # GREEN means nothing needs a look. Any open finding makes it at least YELLOW, so the light, the score and
    # "compliant" always agree; a cleared trigger doesn't count.
    overall_traffic_light = "RED" if blocked else "YELLOW" if (breaches or open_warnings) else "GREEN"
    raw_score = max(0, 100 - sum(v.penalty for v in (*breaches, *unsupported, *to_check)))
    score = raw_score  # a critical rule blocks the post (red) but doesn't cap the score: it is what the points add up to

    recommendations = review_recommendations(blocked, breaches, needs_review, needs_evidence, to_check, rules)

    # Rules with nothing to look for in text (no phrases, no disclaimers) still apply; say so instead of letting a
    # clean score suggest they passed.
    manual = [
        ManualReviewRule(rule_id=r.id, rule_number=r.rule_number, title=r.title, severity=r.severity)
        for r in rules
        if not r.prohibited_terms and not r.required_disclaimers
    ]
    return ComplianceCheckResponse(
        audiences_checked=scope.audiences,
        content_parts=scope.content_parts,
        added_rule_sets=[AddedRuleSet(rule_set=rs, because=w) for rs, w in scope.added_rule_sets.items()],
        manual_review_rules=manual,
        is_compliant=not breaches and not open_warnings,
        traffic_light=overall_traffic_light,
        score=score,
        raw_score=raw_score,
        blocked=blocked,
        ai_unavailable=ai_unavailable,
        score_cap=None,
        violations_count=len(breaches),
        review_count=needs_review,
        evidence_count=needs_evidence,
        cleared_count=cleared,
        violations=violations,
        recommendations=recommendations,
        rules_checked=len(rules),
        rulebook_version=version,
        signals=CheckSignals(
            cashtags=signals.cashtags,
            hashtags=signals.hashtags,
            mentions=signals.mentions,
            links=signals.links,
            government_references=signals.government_references,
            implied_audiences=scope.implied_audiences,
            notes=notes,
        ),
        source_checks=source_checks,
    )
