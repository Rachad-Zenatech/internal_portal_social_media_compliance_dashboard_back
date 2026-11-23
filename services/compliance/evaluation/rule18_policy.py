"""Script/video substantiation is opt-in through sources explicitly cited in the script."""
from services.compliance.evaluation import citation_finder, source_check_service
from services.compliance.media import script_sources

VERSION = "script-citations-1"


def applies(req):
    kind = (req.content_type or "").strip().casefold()
    is_script = kind in {"script", "video_script", "script_doc"}
    if not is_script and kind not in {"video", "video_caption"} and "video" not in req.content_parts:
        return True
    script = req.source_script or (req.text if is_script else "")
    body, sources = script_sources.split(script)
    return bool(citation_finder.find(body) or citation_finder.find(sources))


async def check_sources(req):
    if not applies(req):
        return source_check_service.SourceCheckResult()
    return await source_check_service.run(
        req.text, req.sources, source_script=req.source_script, include_image_citations=False)
