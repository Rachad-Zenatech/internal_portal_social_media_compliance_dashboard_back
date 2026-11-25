"""Explain incomplete review stages without treating missing checks as violations."""
from models.compliance.check_diagnostics_model import IncompleteCheckStage


def stages(semantic_failed, sources, findings, visual_checks):
    out = list(getattr(sources, 'incomplete_stages', []))
    if semantic_failed:
        out.append(IncompleteCheckStage(stage='RULEBOOK', reason='The rulebook meaning check did not complete.'))
    if any(v.evaluation and v.evaluation.source == 'UNAVAILABLE' for v in findings):
        out.append(IncompleteCheckStage(stage='CONTEXT_REVIEW', reason='Some flagged wording could not be evaluated in context.'))
    if any(v.get('status') == 'UNAVAILABLE' for v in visual_checks):
        out.append(IncompleteCheckStage(stage='VISUAL_REVIEW', reason='Visual compliance could not finish for one or more images or videos.'))
    if sources.ai_failed and not getattr(sources, 'incomplete_stages', []):
        out.append(IncompleteCheckStage(stage='SOURCE_VERIFICATION', reason='Claim extraction or source verification did not complete.'))
    return out
