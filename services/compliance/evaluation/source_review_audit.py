"""Keep every searched source outcome inspectable without crowding the source cards."""
import logging
from .source_assertions import identity
from . import newsroom_facts, source_claim_coverage

logger = logging.getLogger(__name__)


def snapshot(readings, *, failed=()):
    audit = []
    for check, _ in readings:
        visible = not newsroom_facts.is_newsroom(check) or check.status == "UNREADABLE" or any(
            c.verdict in {"SUPPORTED", "CONTRADICTED"} for c in check.claims)
        row = {"source": check.title or check.reference, "url": check.url, "status": check.status,
               "visible": visible, "reason": None if visible else "NO_CONFIRMED_MATCH_OR_CONTRADICTION",
               "claims": [{"claim_id": c.claim_id or identity(c.claim, c.where),
                           "model_verdict": c.model_verdict, "verdict": c.verdict,
                           "coverage_complete": source_claim_coverage.claim_key(c.claim, c.where) not in failed,
                           "validation_reason": c.validation_reason} for c in check.claims]}
        audit.append(row)
        logger.info("source_review_outcome", extra={"source_review": row})
    return audit
