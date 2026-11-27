"""Script/video substantiation is opt-in through explicitly supplied or cited sources."""
from services.compliance.evaluation import citation_finder, source_check_service
from services.compliance.media import script_sources
from services.compliance.media.script_content import published

VERSION = "explicit-video-sources-2"


def applies(req):
    kind = (req.content_type or "").strip().casefold()
    is_script = kind in {"script", "video_script", "script_doc"}
    if not is_script and kind not in {"video", "video_caption"} and "video" not in req.content_parts:
        return True
    if any((source.text or "").strip() for source in req.sources):
        return True
    if any(citation.where != "image" for citation in citation_finder.find(req.text)):
        return True
    script = req.source_script or (req.text if is_script else "")
    body, sources = script_sources.split(script)
    return bool(citation_finder.find(body) or citation_finder.find(sources))


async def check_sources(req):
    if getattr(req, "_script_sections", None) is not None:
        req = req.model_copy(update={"text": published(req._script_sections)})
        if not req.text.strip():
            return source_check_service.SourceCheckResult()
    if not applies(req):
        return source_check_service.SourceCheckResult()
    return await source_check_service.run(
        req.text, req.sources, source_script=req.source_script, include_image_citations=False)
