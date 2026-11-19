"""Validation for browser-facing URLs supplied by deployment configuration."""

from urllib.parse import urlsplit


def validate_production_web_config(is_production: bool, cors_origins: list[str], frontend_url: str | None) -> None:
    """Reject insecure or loopback browser URLs before a production server starts."""
    if not is_production:
        return

    values = [*cors_origins, (frontend_url or "").strip()]
    invalid_values = [
        value
        for value in values
        if (parts := urlsplit(value)).scheme != "https"
        or not parts.netloc
        or parts.username
        or parts.password
        or (host := (parts.hostname or "").lower()) == "localhost"
        or host.startswith("localhost.")
        or host.endswith(".localhost")
        or host.startswith("127.")
        or host == "::1"
    ]
    if not cors_origins or invalid_values:
        raise RuntimeError(
            "Production requires HTTPS, non-loopback CORS_ALLOWED_ORIGINS and FRONTEND_URL values"
        )
