"""Check the combined post, retaining caption and image context as the source panel does."""
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
    """Compatibility entry point: identify and verify claims with the whole post in view."""
    body = branding.without_branding(citation_finder.without_citation_text(text)).strip()
    if not IMAGE_MARK.sub("", body).strip():
        return [], None, False
    claims, model = await compare(body, label, title, url, published, source_text)
    return claims, model, claims is None
