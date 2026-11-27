"""Only voiceover and supers may reduce a script's compliance score."""
import re

from services.compliance.media.script_content import PUBLISHED, published


def _normalized(text: str) -> str:
    return " ".join(re.findall(r"\w+", str(text or "").casefold()))


def _locations(text: str, parts: dict[str, str]) -> set[str]:
    needle = _normalized(text)
    return {key for key, value in parts.items() if needle and f" {needle} " in f" {_normalized(value)} "}


def _review_only(finding, locations: set[str]):
    finding.script_review_only = True
    finding.script_sections = sorted(locations or {"details"})
    finding.penalty = finding.uncounted_penalty = finding.extra_claims = 0
    finding.counted_with = None
    if finding.status != "CLEARED":
        finding.status, finding.traffic_light = "NEEDS_REVIEW", "YELLOW"
        finding.explanation += " Production context only: review the final execution; no points are deducted from the script score."
    return finding


def scoring_passage(req, finding, fallback):
    if req._script_sections is None or not finding.script_sections:
        return fallback
    return "\n".join(req._script_sections.get(key, "") for key in finding.script_sections)


def _padded(engine, text):
    text = engine.citation_finder.without_citation_text(engine.branding.without_branding(text))
    signals = engine.post_signals.extract(text)
    return f" {engine._normalize(chr(10).join([text, *signals.hashtag_phrases]))} ", signals.hashtag_keys


async def published_rule_ids(req):
    if req._script_sections is None:
        return None
    from services.compliance.evaluation import compliance_service as engine

    scoped = req.model_copy(update={"text": published(req._script_sections)})
    return {rule.id for rule in await engine.applicable_rules(scoped)}


def rule_findings(req, rules, padded_text, hashtag_keys, disclaimer_padded, scoreable_rules=None):
    """Separate lexical candidates before the shared AI batch judges them, keeping both locations."""
    from services.compliance.evaluation import compliance_service as engine

    groups = [(False, [], padded_text, hashtag_keys)]
    if req._script_sections is not None:
        groups = []
        for review in (False, True):
            keys = [key for key, value in req._script_sections.items() if value and (key not in PUBLISHED) == review]
            text = "\n".join(req._script_sections[key] for key in keys)
            if text.strip():
                padded, tags = _padded(engine, text)
                groups.append((review, keys, padded, tags))
    for rule in rules:
        for review, keys, padded, tags in groups:
            if not review and scoreable_rules is not None and rule.id not in scoreable_rules:
                continue
            if finding := engine._evaluate_rule(rule, padded, tags, disclaimer_padded):
                finding.script_sections = keys
                finding.script_review_only = review
                yield finding, rule


def apply_policy(violations: list, parts: dict[str, str]) -> None:
    """Use judged evidence before generic trigger words, which may occur in several columns."""
    extra = []
    for finding in violations:
        if finding.script_review_only:
            _review_only(finding, set(finding.script_sections))
            continue
        if set(finding.script_sections) & PUBLISHED:
            continue
        evidence = [finding.evaluation.matched_text if finding.evaluation else None, finding.passage]
        location = next((where for quote in evidence if quote and (where := _locations(quote, parts))), set())
        if location:
            if not location & PUBLISHED:
                _review_only(finding, location)
            else:
                finding.script_sections = sorted(location & PUBLISHED)
            continue
        located = [(term, _locations(term, parts)) for term in finding.flagged_terms]
        scored = [(term, where) for term, where in located if where & PUBLISHED]
        review = [(term, where) for term, where in located if not where & PUBLISHED]
        if scored:
            finding.script_sections = sorted(set().union(*(where & PUBLISHED for _, where in scored)))
            if review:
                note = finding.model_copy(deep=True, update={"flagged_terms": [term for term, _ in review]})
                extra.append(_review_only(note, set().union(*(where for _, where in review))))
                finding.flagged_terms = [term for term, _ in scored]
        elif finding.kind == "MISSING_DISCLAIMER" and not finding.flagged_terms and published(parts):
            finding.script_sections = sorted(key for key in PUBLISHED if parts.get(key))
        else:
            _review_only(finding, set().union(*(where for _, where in located)))
    violations.extend(extra)


def score_findings(req, violations, rules, padded_text, scoreable_rules=None):
    """Preserve normal post scoring; constrain all script penalty and deduplication paths."""
    from services.compliance.evaluation import compliance_service as engine

    if req._script_sections is not None:
        contextual_only = {rule.id for rule in rules} - (scoreable_rules or set()) if scoreable_rules is not None else set()
        for finding in violations:
            if finding.rule_id in contextual_only:
                finding.script_review_only = True
        apply_policy(violations, req._script_sections)
        padded_text, _ = _padded(engine, published(req._script_sections))
    eligible = [finding for finding in violations if not finding.script_review_only]
    breaches = [finding for finding in eligible if finding.status == "VIOLATION"]
    engine._count_overlaps_once(breaches, padded_text)
    engine._count_extra_claims(breaches, rules, padded_text)
    return breaches, engine._charge_unsupported_claims(eligible), engine._charge_review_findings(eligible, rules)


async def verify_script_sources(req, findings, rules):
    """Keep script source verification independent of the finished-post source policy."""
    if getattr(req, "_script_sections", None) is not None:
        from .script_source_checks import check
        return await check(req)
    from .source_evidence_lookup import check
    return await check(req, findings, rules)
