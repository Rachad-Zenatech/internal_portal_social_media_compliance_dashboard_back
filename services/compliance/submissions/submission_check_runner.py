"""Run and snapshot the complete post check without changing its scoring policy."""

from datetime import datetime, timezone
from typing import Any, Dict, Tuple

from services.compliance.evaluation import check_rule_capture, compliance_check_result
from services.compliance.submissions import check_snapshot_service


async def run_check(row: Dict[str, Any]) -> Tuple[Any, Dict[str, Any]]:
    """The one rulebook check every screen shows: title, caption, the script (or a blog's article file), the video
    transcript and the text shown in the post's images (each once it's been read), against the rules scoped to this
    submission. Includes timeline flags and check coverage."""
    from services.compliance.submissions import submission_service as subs

    material = await subs.submission_check_material.load(row)
    video, images, documents = material.video, material.images, material.documents
    script, segments, visual_segments = material.script, material.segments, material.visual_segments
    video_scored = material.video_scored
    request = subs.check_request(row, material.text, has_video=bool(video), has_images=bool(images), sources=material.sources,
                                 disclaimer_text=material.published, source_script=material.source_script or None,
                                 visual_media=[*images, *([video] if video else [])])
    post, evaluated = check_snapshot_service.inputs(row, request, material=material)
    with check_rule_capture.collecting() as captured:
        result = await subs.check_compliance(request)
    all_video_segments = [*segments, *visual_segments]
    non_image_text = material.text.split("Text in image ", 1)[0]
    result.source_regions = await subs.video_finding_service.locate_media_findings(
        images, video, all_video_segments, result.violations, non_image_text=non_image_text
    )
    compliance_check_result.recalculate_totals(result)
    match = subs.script_match.assess_video(script, " ".join(str(s.get("text", "")) for s in segments if s.get("source") != "on_screen"), video, on_screen=" ".join(str(s.get("text", "")) for s in segments if s.get("source") == "on_screen"))
    subs.script_match.flag_mismatch(result, match)
    subs.source_recheck.record_cleared(result, (subs._json(row.get("latest_check")) or {}).get("violations") or [])
    check = {
        **result.model_dump(mode="json"),
        "checked_at": datetime.now(timezone.utc).isoformat(),
        "checked_source_links": subs.submission_source_state.snapshot(row),
        "checked_media_edited_at": str(row["media_edited_at"]) if row.get("media_edited_at") else None,
        "includes_transcript": bool(segments),
        "includes_visual_text": bool(visual_segments),
        # A video whose transcript isn't ready yet is re-checked automatically when it is.
        "transcript_pending": bool(video) and video["transcript_status"] not in ("COMPLETED", "FAILED"),
        # Image reads still pending are re-checked automatically when done.
        "images_total": len(images),
        "images_read": sum(1 for i in images if i["image_text_status"] == "COMPLETED"),
        "images_pending": sum(1 for i in images if i["image_text_status"] in (None, "PROCESSING")),
        "images_failed": sum(1 for i in images if i["image_text_status"] == "FAILED"),
        # The same for the script (and a blog's article file). Failed ones say why, so the creator can fix it.
        "includes_script": not video_scored and (
            bool(script) or any(d["kind"] == "SCRIPT" and d["doc_text_status"] == "COMPLETED" for d in documents)
        ),
        "documents_total": len(documents),
        "documents_read": sum(1 for d in documents if d["doc_text_status"] == "COMPLETED"),
        "documents_pending": sum(1 for d in documents if d["doc_text_status"] in (None, "PROCESSING")),
        "documents_failed": [
            {"name": d["original_name"], "error": d["doc_text_error"]}
            for d in documents if d["doc_text_status"] == "FAILED"
        ],
        "segment_flags": subs.flag_segments(all_video_segments, result.violations),
        # Everything that couldn't be read, so its wording was never checked. Approval waits for these (see reads_failed).
        "reads_failed": subs.media_read_status.failed_reads(video, images, documents),
        # No score when AI is unavailable; re-run the check once it is back.
        "ai_unavailable": result.ai_unavailable,
        "script_match": match,
    }
    # No score until the whole post has been read; screens show "Under review" until then.
    check["analysis_pending"] = subs.analysis_pending(check)
    check = await check_snapshot_service.save(post, evaluated, captured, check)
    return result, check
