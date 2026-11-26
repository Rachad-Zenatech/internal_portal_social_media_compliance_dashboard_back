"""Assemble context reviews, including rules that always require a contextual decision."""
from . import army_context_review, context_evaluator, forward_looking_context


def for_post(contextual, req, rules, findings):
    candidates = [context_evaluator.Candidate(v, rule, v.passage or "", v.context or "") for v, rule in contextual]
    army_context_review.include(req, rules, findings, candidates)
    forward_looking_context.include(rules, findings, candidates, req.text)
    return candidates
