"""The admin dashboard's SendGrid configuration, with social portal URLs and team time."""
import os
from datetime import datetime
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
from services.notifications import email_service


def team_timezone() -> ZoneInfo:
    return ZoneInfo(os.getenv("REVIEW_EMAIL_TIMEZONE") or os.getenv("REVIEW_TIMEZONE") or "America/Toronto")


def user_timezone(user: dict) -> ZoneInfo:
    try:
        return ZoneInfo(user["time_zone"]) if user.get("time_zone") else team_timezone()
    except (ValueError, ZoneInfoNotFoundError):
        return team_timezone()


def portal_url(path: str) -> str:
    base = (os.getenv("FRONTEND_URL") or "").strip().rstrip("/")
    parsed = urlsplit(base)
    if parsed.scheme not in ("https", "http") or not parsed.netloc or parsed.username or parsed.password:
        raise ValueError("Set FRONTEND_URL to the social media portal's full URL before enabling email")
    if not path.startswith("/") or path.startswith("//"):
        raise ValueError("Email links must use a portal-relative path")
    return base + path


def enabled() -> bool:
    return os.getenv("REVIEW_EMAIL_ENABLED", "true").lower() not in {"false", "0", "no", "off"}


def configured() -> bool:
    if not enabled() or not email_service.get_sendgrid_api_key():
        return False
    try:
        portal_url("/")
        team_timezone()
        return True
    except (ValueError, KeyError, ZoneInfoNotFoundError):
        return False


def when(value: datetime | None, time_zone: str | None = None) -> str:
    zone = ZoneInfo(time_zone) if time_zone else team_timezone()
    return value.astimezone(zone).strftime("%a, %b %d at %I:%M %p %Z") if value else "No deadline"
