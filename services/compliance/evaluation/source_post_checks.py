"""Compare each published part separately so caption claims cannot crowd out image claims."""
from models.compliance.compliance_model import SourceClaim
from .image_marks import IMAGE_MARK
from . import source_image_claims


def parts(text):
    marks = list(IMAGE_MARK.finditer(text))
    if not marks:
        return [(text, False)]
    out = [(text[:marks[0].start()].strip(), False)]
    for index, mark in enumerate(marks):
        end = marks[index + 1].start() if index + 1 < len(marks) else len(text)
        out.append((text[mark.start():end].strip(), True))
    return [(body, image) for body, image in out if body]


async def complete_image_claims(body, claims, arguments, compare):
    required = source_image_claims.labels(body)
    missing = [claim for claim in required if not any(source_image_claims.covers(claim, r.claim) for r in claims)]
    extra, models, failed = [], [], False
    for start in range(0, len(missing), 12):
        batch = missing[start:start + 12]
        focused = IMAGE_MARK.search(body).group() + "\n" + "\n".join(f"- {claim}" for claim in batch)
        results, model = await compare(focused, *arguments)
        if results is None:
            failed = True
        else:
            extra.extend(r for r in results if any(source_image_claims.covers(claim, r.claim) for claim in batch))
        if model and model not in models:
            models.append(model)
    for claim in missing:
        if not any(source_image_claims.covers(claim, r.claim) for r in extra):
            extra.append(SourceClaim(claim=claim, where="image", verdict="UNCLEAR", confidence=0,
                note="This capability was omitted by the source comparison. Confirm it against a specific supporting passage."))
    return extra, models, failed


async def compare_parts(text, label, title, url, published, source_text, compare):
    """Keep every part's findings and expose any part no model could compare."""
    combined, models, seen = [], [], set()
    completed, failed = False, False
    for body, image in parts(text):
        claims, model = await compare(body, label, title, url, published, source_text)
        if claims is None:
            failed = True
            continue
        completed = True
        if model and model not in models:
            models.append(model)
        if image:
            claims = source_image_claims.guard_claims(body, claims, source_text)
            extra, extra_models, incomplete = await complete_image_claims(
                body, claims, (label, title, url, published, source_text), compare)
            claims = [*claims, *extra]
            models.extend(model for model in extra_models if model not in models)
            failed = failed or incomplete
        for claim in claims:
            where = "image" if image else "caption"
            key = (" ".join(claim.claim.casefold().split()), where)
            if key not in seen:
                seen.add(key)
                combined.append(claim.model_copy(update={"where": where}))
    return combined if completed else None, ", ".join(models) or None, failed
