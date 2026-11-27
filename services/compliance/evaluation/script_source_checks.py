"""Verify every published script claim against its citations and the company newsroom."""
from services.compliance.evaluation import source_check_service
from services.compliance.media.script_content import PUBLISHED, published


async def check_sources(req):
    text = published(req._script_sections)
    if not text:
        return source_check_service.SourceCheckResult()
    # The reader always retrieves newsroom evidence. Supplied sources add independent comparisons.
    return await source_check_service.run(
        text, req.sources, source_script=req.source_script, include_image_citations=False)


async def check(req):
    result = await check_sources(req)
    issues = source_check_service.findings(result.checks, failed=result.failed_claims)
    issues.extend(result.unbacked)
    for issue in issues:
        quotes = [issue.passage, *issue.flagged_terms]
        locations = [key for key in PUBLISHED if any(
            quote and quote.casefold() in req._script_sections.get(key, "").casefold() for quote in quotes)]
        # Every finding came from published copy, even when an atomic claim is phrased differently.
        issue.script_sections = sorted(locations or [key for key in PUBLISHED if req._script_sections.get(key)])
    return result, issues
