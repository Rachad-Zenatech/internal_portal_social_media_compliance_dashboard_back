"""Bind source judgments to an existing inventory; sources cannot rewrite or discover claims."""
from . import claim_payload
from models.compliance.compliance_model import SourceClaim

INSTRUCTION = """Verify ONLY the supplied claims, by claim_id. Return one result for EVERY ID, including UNCLEAR
when this source is unrelated or insufficient. Do not add claims, alter wording, infer a company subject from branding,
or strip qualifications. The inventory's subject/scope/qualification are binding. No factual claim means no result.
NOT_FOUND means this source does not establish the claim; it does not mean false. CONTRADICTED requires affirmative
incompatible evidence, not silence. For an ambiguous subject or extraction, return UNCLEAR without a deduction.
"""


async def compare(manifest, arguments, reader):
    out, models, failed = [], [], False
    by_id = {c.id: c for c in manifest.claims}
    for group in claim_payload.batches([c.model_dump() for c in manifest.claims]):
        batch = [by_id[c["id"]] for c in group]
        payload = claim_payload.payload(manifest.published_text, "claims", group, INSTRUCTION + "\n")
        results, model = await reader(payload, *arguments) if payload is not None else (None, None)
        if model and model not in models:
            models.append(model)
        failed = failed or results is None
        indexed = {}
        for result in results or []:
            key = getattr(result, "claim_id", None)
            # Compatibility with models returning exact wording instead of the requested ID; no fuzzy replacement.
            if not key:
                key = next((c.id for c in batch if result.claim == c.wording), None)
            if key not in indexed:
                indexed[key] = result
        for claim in batch:
            result = indexed.get(claim.id)
            where = claim.locations[0].where
            rewritten = result is not None and result.claim and " ".join(result.claim.casefold().split()) != " ".join(claim.wording.casefold().split())
            if result is None or claim.ambiguous or rewritten:
                note = ("Clarify the subject or factual meaning before checking evidence." if claim.ambiguous else
                        "This claim was not verified in the source response; check again or confirm the evidence.")
                out.append(SourceClaim(claim=claim.wording, claim_id=claim.id, where=where, verdict="UNCLEAR", note=note, clarification_needed=claim.ambiguous))
                if rewritten:
                    note = "The source response changed the original assertion, so its judgment was not applied."
                    out[-1].note = note
                failed = failed or result is None or bool(rewritten)
            else:
                # Preserve evidence judgments, never source-model paraphrases of the published claim.
                out.append(SourceClaim(claim=claim.wording, claim_id=claim.id, where=where,
                    verdict=result.verdict if result.verdict in {"SUPPORTED", "NOT_FOUND", "CONTRADICTED", "UNCLEAR"} else "UNCLEAR",
                    evidence=result.evidence, source_location=result.source_location, note=result.note,
                    confidence=result.confidence))
    return out, ", ".join(models) or None, failed
