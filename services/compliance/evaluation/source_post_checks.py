"""Compare each published part separately so caption claims cannot crowd out image claims."""
from .image_marks import IMAGE_MARK
from . import citation_finder, branding


def parts(text):
    text = branding.without_branding(citation_finder.without_citation_text(text))
    marks = list(IMAGE_MARK.finditer(text))
    if not marks:
        return [(text, False)]
    out = [(text[:marks[0].start()].strip(), False)]
    for index, mark in enumerate(marks):
        end = marks[index + 1].start() if index + 1 < len(marks) else len(text)
        out.append((text[mark.start():end].strip(), True))
    return [(body, image) for body, image in out if body]


def unsourced_image_findings(text, checks):
    """Labels alone cannot establish a factual assertion requiring evidence."""
    return []


async def compare_parts(text, label, title, url, published, source_text, compare):
    """Keep every part's findings and expose any part no model could compare."""
    combined, models, seen = [], [], set()
    completed, failed = False, False
    for body, image in parts(text):
        if not IMAGE_MARK.sub("", body).strip():
            continue
        claims, model = await compare(body, label, title, url, published, source_text)
        if claims is None:
            failed = True
            continue
        completed = True
        if model and model not in models:
            models.append(model)
        for claim in claims:
            where = "image" if image else "caption"
            key = (" ".join(claim.claim.casefold().split()), where)
            if key not in seen:
                seen.add(key)
                combined.append(claim.model_copy(update={"where": where}))
    return combined if completed or not failed else None, ", ".join(models) or None, failed
