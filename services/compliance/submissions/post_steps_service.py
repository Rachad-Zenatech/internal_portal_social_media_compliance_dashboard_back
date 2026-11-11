"""The five steps a creator works through, and how far each post has got.

    1. details     the post is filled in: title, brand, audience, accounts, caption, media (and its script, for a video)
    2. compliance  the rulebook check has been run on the post as it is now
    3. sentiment   the sentiment analysis has been run on the post as it is now
    4. mood        the public mood has been read for the post as it is now
    5. send        it has been sent for review

A tool counts as done only while it is up to date: if the post's content changed after the tool ran (see
content_edited_at), it is "stale" and has to be run again. A post can be sent for review only when steps 1 to 4 are all
done, and once it is sent (in review, approved or published) it is locked: every step shows as done and nothing can be
edited or re-run, until a reviewer asks for changes.

"My posts" shows this as five dots on each post, and a click on the post opens the step it was left on (`next`).
"""

from datetime import datetime, timezone
from typing import Any, List, Mapping, Optional

from models.compliance.submission_model import SubmissionSteps

# Statuses the creator can still change; anything else is locked.
EDITABLE_STATUSES = ("DRAFT", "CHANGES_REQUESTED")

# What a missing detail is called to the creator.
FIELD_WORDS = {
    "title": "title", "brand_code": "brand", "audience": "audience", "region": "region", "platforms": "platform",
    "account_id": "account to post on", "rule_sets": "rule set", "caption": "caption", "main_file": "image or video",
    "script_approval": "approved script", "script": "script",
}


def missing_words(missing: List[str]) -> str:
    return ", ".join(FIELD_WORDS.get(m, m) for m in missing)


def _aware(value: Optional[datetime]) -> Optional[datetime]:
    if value is None:
        return None
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


def details_missing(r: Mapping[str, Any]) -> List[str]:
    """What the post still needs before its details are complete. The fields that belong to sending (reviewers, the date
    approval is needed by) are not part of this step."""
    missing: List[str] = []
    if not (r.get("title") or "").strip():
        missing.append("title")
    if not (r.get("brand_codes") or r.get("brand_code")):
        missing.append("brand_code")
    if not (r.get("audiences") or r.get("audience")):
        missing.append("audience")
    if not r.get("region"):
        missing.append("region")
    if not r.get("platforms"):
        missing.append("platforms")
    if not r.get("has_account"):
        missing.append("account_id")
    if not r.get("rule_sets"):
        missing.append("rule_sets")
    if not (r.get("caption_text") or "").strip():
        missing.append("caption")
    # Types that carry media need it; a text post doesn't. A social post takes images only, so a video on one is a gap.
    if r.get("main_required") and not r.get("has_main"):
        missing.append("main_file")
    elif r.get("content_type") == "SOCIAL_POST" and r.get("main_is_video"):
        missing.append("main_file")
    if r.get("script_stage") and r.get("script_status") != "APPROVED":
        missing.append("script_approval")
    return missing


def _tool(done_at: Optional[datetime], edited_at: Optional[datetime], running: bool = False) -> str:
    """done, stale (run before the last edit), running, or todo (never run)."""
    if running:
        return "running"
    if done_at is None:
        return "todo"
    done_at, edited_at = _aware(done_at), _aware(edited_at)
    return "stale" if edited_at and done_at < edited_at else "done"


def compute_steps(r: Mapping[str, Any]) -> SubmissionSteps:
    """The steps for one post, from a row of what the list query reads (see submission_service.list_submissions)."""
    status = r.get("status")
    locked = status not in EDITABLE_STATUSES
    missing = details_missing(r)
    if locked:
        # Sent: everything was done when it went. Nothing to run and nothing to send.
        return SubmissionSteps(details="done", compliance="done", sentiment="done", mood="done", send="done",
                               next=None, ready_to_send=False, locked=True, missing=[])

    edited_at = r.get("content_edited_at") or r.get("created_at")
    details = "todo" if missing else "done"

    # The rulebook check: scored and not older than the last edit. A check made before checks recorded their time
    # (no checked_at) counts as current, because there is nothing to compare it with.
    if r.get("analysis_pending"):
        compliance = "running"
    elif r.get("ai_unavailable") or (r.get("latest_score") is None and not r.get("check_at")):
        compliance = "todo"
    elif r.get("check_at") is None:
        compliance = "done"
    else:
        compliance = _tool(r["check_at"], edited_at)

    sentiment = _tool(r.get("sentiment_at"), edited_at, bool(r.get("sentiment_running")))
    mood = _tool(r.get("mood_at"), edited_at)

    done = {"details": details, "compliance": compliance, "sentiment": sentiment, "mood": mood}
    next_step = next((k for k in ("details", "compliance", "sentiment", "mood") if done[k] != "done"), "send")
    ready = all(v == "done" for v in done.values())
    return SubmissionSteps(
        details=details, compliance=compliance, sentiment=sentiment, mood=mood, send="todo",
        next=next_step, ready_to_send=ready, locked=False, missing=missing,
    )
