"""Caption suggestions: a few ready-to-edit captions drafted from the post itself.

Gemini sees what a reviewer would: the images, the video itself (watched, not just its transcript), the video's
transcript and on-screen text, the title, and who it's for. It writes in the brand's voice and the platforms' norms (Messaging Context), steers clear of what the rulebook
prohibits, and only states what the content shows, so it never invents numbers, partners or claims. Every
suggestion is then run through the compliance check, so the writer sees its score before using it.

Nothing is saved; the writer picks one, edits it, and it goes through the normal checks with the rest of the post.
"""

import json
import logging
from typing import Any, Dict, List
from uuid import UUID

from fastapi import HTTPException
from google.genai import types
from pydantic import ValidationError

from models.caption_model import CaptionCheck, CaptionSuggestion, CaptionSuggestions, CaptionSuggestRequest
from models.compliance_model import ComplianceCheckRequest
from services import ai_usage_service, context_service
from services import sentiment_service as sentiment
from services.compliance_service import applicable_rules, check_compliance
from services.gemini_service import generate_content_with_fallback

logger = logging.getLogger(__name__)

MAX_IMAGES = 6
MAX_TRANSCRIPT_CHARS = 8000
MAX_RULES = 25
# Captions are short and sweet: a sentence or two before the hashtags, and a few hashtags at most.
CAPTION_MAX_CHARS = 150
MAX_HASHTAGS = 3

RESPONSE_SCHEMA: Dict[str, Any] = {
    "type": "OBJECT",
    "properties": {
        "suggestions": {
            "type": "ARRAY",
            "items": {
                "type": "OBJECT",
                "properties": {
                    "style": {"type": "STRING"},
                    "text": {"type": "STRING"},
                    "hashtags": {"type": "ARRAY", "items": {"type": "STRING"}},
                },
                "required": ["style", "text", "hashtags"],
            },
        },
        "notes": {"type": "ARRAY", "items": {"type": "STRING"}},
    },
    "required": ["suggestions", "notes"],
}


def _transcript_text(row: Dict[str, Any]) -> tuple[str, str]:
    """(spoken words, on-screen text) from the video's transcript, if it's ready."""
    if not row.get("video_id") or row.get("transcript_status") != "COMPLETED":
        return "", ""
    segments = sentiment._json(row["transcript"]) or []
    spoken = " ".join(s["text"] for s in segments if s.get("source", "speech") != "on_screen")
    on_screen = " | ".join(s["text"] for s in segments if s.get("source") == "on_screen")
    return spoken[:MAX_TRANSCRIPT_CHARS], on_screen[:2000]


def _prompt(row: Dict[str, Any], req: CaptionSuggestRequest, spoken: str, on_screen: str, images: int,
            context: List[Dict[str, Any]], rules: List[Any], video_attached: bool = False) -> str:
    meta = sentiment._json(row["account_meta"]) or {}
    is_ceo = bool(meta.get("is_ceo")) or row["brand_code"] == sentiment.CEO_BRAND
    rule_lines = "\n".join(
        f"- {r.rule_number} {r.title}: {r.summary[:300]}"
        + (f" Never use: {', '.join(r.prohibited_terms[:15])}." if r.prohibited_terms else "")
        for r in rules[:MAX_RULES]
    ) or "- (none selected)"
    existing = (row["caption_text"] or "").strip()
    return f"""You write social media captions for a publicly listed drone and technology company. Draft 3 short, sweet
captions for the post below, each in a clearly different tone (for example: warm, playful, or ending on a question).

Hard rules:
- Short and sweet: every caption is 1 or 2 sentences and at most {CAPTION_MAX_CHARS} characters before hashtags. A
  caption adds a hook or feeling to the post; it doesn't retell it. Never recap the video scene by scene, list who
  says what, or name people in it.
- Build each caption on what the content actually shows: {"watch the attached video for its real subject, action and setting; " if video_attached else ""}use the images, the transcript and on-screen text. The caption must make sense to someone watching the post.
- The title is the writer's working label (it may say "test", "draft", "v2" or similar). Use it only as a hint; never
  build the caption around title words the content doesn't show.
- Say only what the post's content shows or states. Do not invent numbers, dates, locations, customers, partners,
  certifications, availability or results. If something would help but isn't there, leave it out and mention it in
  notes.
- If there's too little to go on (no video or images could be read and there's no transcript), say so in notes and
  keep the captions short and general rather than guessing.
- No benefit claims the content doesn't make: words like efficient, faster, safer, cheaper, reliable, cutting-edge,
  leading or innovative need the content to say so.
- Write the caption that goes with the post, not a description of it: never "this image shows", "pictured here",
  "in this video" or "as seen here". The reader sees the image or video already.
- The style names are short and capitalised, e.g. "Warm", "Playful", "Question".
- Follow the compliance rules below: no prohibited wording, no absolute or guaranteed claims, no forward-looking
  promises about performance, revenue or stock price, no implied government or regulatory endorsement.
- Some rules require a disclaimer only when a post says certain things (e.g. a safe-harbor note for forward-looking
  statements). Write captions that don't need one; never add a disclaimer to a caption that doesn't trigger it.
- Match the brand voice and the platform norms in the reference context.
- Each caption plus its hashtags must fit in {req.max_chars} characters. Up to {MAX_HASHTAGS} relevant hashtags, without
  spaces, each starting with #. Don't repeat hashtags inside the caption text.
- {"Write in the first person as the executive whose channel this is." if is_ceo else "Write as the company or brand."}
- Plain text only: no markdown, no surrounding quotes. Emoji only if the platform norms suit them, and sparingly.
- notes: short points for the writer, e.g. a fact worth adding that the content doesn't show. Empty if none.
{f'- The writer asked: "{req.hint.strip()}". Follow it unless it conflicts with the rules above.' if req.hint and req.hint.strip() else ""}

POST
Title: {row["title"] or "(none)"}
Content type: {row["content_type"]}
Brand: {row["brand_name"] or row["brand_code"] or "unknown"}
Audience: {row["audience"] or "general"}
Platforms: {", ".join(row["platforms"] or []) or "unspecified"}
Publishing channel: {row["account_name"] or "unknown"}
Images attached above: {images}
Video attached above: {"yes, watch it" if video_attached else "no"}
Video transcript (spoken): {spoken or "(no video, or the transcript isn't ready)"}
Text shown on screen in the video: {on_screen or "(none)"}
Current caption (improve on it if useful): {existing or "(none)"}

COMPLIANCE RULES FOR THIS POST
{rule_lines}

{sentiment._reference_context(context)}
"""


async def _check(text: str, row: Dict[str, Any]) -> CaptionCheck:
    result = await check_compliance(ComplianceCheckRequest(
        text=text,
        video_title=row["title"],
        content_type="social_post",
        rule_sets=list(row["rule_sets"] or []),
        brand_code=row["brand_code"],
        audience=row["audience"],
        region=row["region"],
        platforms=list(row["platforms"] or []),
    ))
    return CaptionCheck(
        score=result.score,
        traffic_light=result.traffic_light,
        blocked=result.blocked,
        flags=[
            f"{v.rule_number}: {', '.join(v.flagged_terms) if v.flagged_terms else v.title}" for v in result.violations
        ],
    )


async def suggest(submission_id: UUID, user_id: UUID, req: CaptionSuggestRequest) -> CaptionSuggestions:
    row = await sentiment._load_submission(submission_id)
    if not row:
        raise HTTPException(status_code=404, detail="Submission not found")
    row = dict(row)
    spoken, on_screen = _transcript_text(row)
    image_refs = (await sentiment._load_images(submission_id))[:MAX_IMAGES]
    if not (row["title"] or "").strip() and not image_refs and not spoken and not on_screen:
        raise HTTPException(
            status_code=422,
            detail="Add the post (an image or video) or a title first, so there's something to write a caption about.",
        )

    await ai_usage_service.reserve("CAPTION", user_id, submission_id)
    images = await sentiment._image_parts(image_refs)
    platforms = list(row["platforms"] or [])
    context = await context_service.for_content(row["brand_code"], row["audience"], platforms)
    rules = await applicable_rules(ComplianceCheckRequest(
        text="-", rule_sets=list(row["rule_sets"] or []), brand_code=row["brand_code"], audience=row["audience"],
        region=row["region"], platforms=platforms,
    ))
    client = sentiment._get_client()
    # Let the model watch the video, as the sentiment analysis does: a transcript alone says nothing about a video
    # with music and no speech, and the captions then end up written from the title.
    video = await sentiment._upload_video(client, str(row["video_id"])) if row.get("video_id") else None
    prompt = _prompt(row, req, spoken, on_screen, len(images), context, rules, video_attached=video is not None)
    contents: Any = prompt
    if images or video:
        contents = []
        for n, part in enumerate(images, start=1):
            contents += [f"IMAGE {n}:", part]
        if video:
            contents += ["VIDEO:", video]
        contents.append(prompt)
    config = types.GenerateContentConfig(
        response_mime_type="application/json",
        response_schema=RESPONSE_SCHEMA,
        temperature=0.7,
    )
    try:
        response = await generate_content_with_fallback(client, contents, config, feature="CAPTION", submission_id=submission_id)
        data = json.loads(response.text or "{}")
        raw = data.get("suggestions") or []
    except (ValueError, ValidationError) as e:
        logger.warning("Caption suggestions came back malformed: %s", e)
        raise HTTPException(status_code=502, detail="The suggestions came back malformed. Try again.")
    except RuntimeError as e:
        logger.warning("Caption suggestions failed: %s", e)
        raise HTTPException(status_code=503, detail="The AI service is busy or out of quota. Try again later.")
    finally:
        if video:
            await sentiment._delete_upload(client, video)

    out = CaptionSuggestions(model=getattr(response, "model_version", None), notes=list(data.get("notes") or [])[:4])
    for s in raw[:3]:
        text = str(s.get("text") or "").strip().strip('"').strip()
        if not text:
            continue
        tags = []
        for t in s.get("hashtags") or []:
            t = "#" + str(t).strip().lstrip("#").replace(" ", "")
            if len(t) > 1 and t.lower() not in {x.lower() for x in tags}:
                tags.append(t)
        tags = tags[:MAX_HASHTAGS]
        full = text + ("\n\n" + " ".join(tags) if tags else "")
        # Trim hashtags rather than the caption when it runs over the limit.
        while tags and len(full) > req.max_chars:
            tags.pop()
            full = text + ("\n\n" + " ".join(tags) if tags else "")
        style = str(s.get("style") or "Suggestion").strip()[:40]
        out.suggestions.append(CaptionSuggestion(
            style=style[:1].upper() + style[1:],
            text=text,
            hashtags=tags,
            characters=len(full),
            check=await _check(full, row),
        ))
    if not out.suggestions:
        raise HTTPException(status_code=502, detail="No usable suggestions came back. Try again.")

    if images:
        out.based_on.append(f"{len(images)} image{'s' if len(images) != 1 else ''}")
    if video:
        out.based_on.append("the video")
    if spoken:
        out.based_on.append("the video transcript")
    if on_screen:
        out.based_on.append("the video's on-screen text")
    if (row["title"] or "").strip():
        out.based_on.append("the title")
    if any(e["kind"] == "BRAND_VOICE" for e in context):
        out.based_on.append("the brand voice")
    if row["video_id"] and not video:
        out.notes.insert(
            0,
            "The video couldn't be attached, so the captions are based on "
            + ("its transcript." if spoken or on_screen else "the title only. Try again in a moment."),
        )
    if len(image_refs) > len(images):
        out.notes.insert(0, f"Only {len(images)} of {len(image_refs)} images could be read.")
    return out
