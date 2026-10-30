"""Email service module using SendGrid REST API for Social Media Compliance & Notifications.
"""

import logging
import os
from typing import Any, Dict, List, Optional, Union
import httpx

logger = logging.getLogger(__name__)


def get_default_mailer() -> str:
    return (os.getenv("MAIL_FROM_ADDRESS") or "alvin.tsang@zenatech.com").strip()


def get_default_sender_name() -> str:
    return (os.getenv("MAIL_FROM_NAME") or "ZenaTech Social Media & Compliance").strip()


SENDGRID_API_URL = "https://api.sendgrid.com/v3/mail/send"


def get_sendgrid_api_key() -> Optional[str]:
    """Retrieve SendGrid API key from environment."""
    key = os.getenv("SENDGRIP_API_KEY") or os.getenv("SENDGRID_API_KEY")
    if key:
        return key.strip()
    return None


async def send_email(
    to_emails: Union[str, List[str]],
    subject: str,
    html_content: str,
    text_content: Optional[str] = None,
    from_email: Optional[str] = None,
    from_name: Optional[str] = None,
) -> bool:
    """Send an email via SendGrid v3 API.

    Returns True if accepted (HTTP 200 or 202), False otherwise.
    Never throws unhandled exceptions to avoid interrupting calling workflows.
    """
    sender_email = (from_email or get_default_mailer()).strip()
    sender_name = (from_name or get_default_sender_name()).strip()
    api_key = get_sendgrid_api_key()
    if not api_key:
        logger.warning(
            "SendGrid email skipped: Neither SENDGRIP_API_KEY nor SENDGRID_API_KEY is configured in environment."
        )
        return False

    recipients: List[str] = []
    if isinstance(to_emails, str):
        recipients = [e.strip() for e in to_emails.split(",") if e.strip()]
    elif isinstance(to_emails, (list, tuple, set)):
        recipients = [str(e).strip() for e in to_emails if str(e).strip()]

    recipients = [e for e in recipients if "@" in e]
    if not recipients:
        logger.info("SendGrid email skipped: No valid recipient email addresses.")
        return False

    app_env = (os.getenv("APP_ENV") or "development").lower()
    if app_env != "production" and not subject.startswith("[TEST]"):
        subject = f"[TEST] {subject}"

    content_list = []
    if text_content:
        content_list.append({"type": "text/plain", "value": text_content})
    if html_content:
        content_list.append({"type": "text/html", "value": html_content})
    elif not content_list:
        content_list.append({"type": "text/plain", "value": subject})

    payload: Dict[str, Any] = {
        "personalizations": [
            {
                "to": [{"email": email} for email in recipients]
            }
        ],
        "from": {
            "email": sender_email,
            "name": sender_name,
        },
        "subject": subject,
        "content": content_list,
    }

    is_sandbox = os.getenv("SENDGRID_SANDBOX_MODE", "false").lower() in ("true", "1", "yes")
    if is_sandbox:
        payload["mail_settings"] = {
            "sandbox_mode": {
                "enable": True
            }
        }

    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }

    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.post(SENDGRID_API_URL, json=payload, headers=headers)
            if 200 <= resp.status_code < 300:
                logger.info(
                    "SendGrid email sent successfully to %s: '%s' (status %s)",
                    recipients,
                    subject,
                    resp.status_code,
                )
                return True
            else:
                logger.error(
                    "SendGrid email delivery failed (status %s): %s",
                    resp.status_code,
                    resp.text,
                )
                return False
    except Exception as exc:
        logger.error("SendGrid email exception sending to %s: %s", recipients, exc)
        return False


def _render_base_email(title: str, preheader: str, body_html: str, action_url: Optional[str] = None, action_label: str = "View in Dashboard") -> str:
    """Renders standard ZenaTech branded HTML email template."""
    action_btn_html = ""
    if action_url:
        action_btn_html = f"""
        <table role="presentation" border="0" cellpadding="0" cellspacing="0" style="margin-top:24px;margin-bottom:24px;">
            <tr>
                <td align="center" style="border-radius:6px;background-color:#4f46e5;">
                    <a href="{action_url}" target="_blank" style="display:inline-block;padding:12px 24px;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif;font-size:14px;font-weight:600;color:#ffffff;text-decoration:none;border-radius:6px;">
                        {action_label} &rarr;
                    </a>
                </td>
            </tr>
        </table>
        """

    return f"""<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>{title}</title>
</head>
<body style="margin:0;padding:0;background-color:#f8fafc;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,Helvetica,Arial,sans-serif;color:#1e293b;">
    <div style="display:none;font-size:1px;color:#f8fafc;line-height:1px;max-height:0px;max-width:0px;opacity:0;overflow:hidden;">
        {preheader}
    </div>
    <table role="presentation" width="100%" border="0" cellpadding="0" cellspacing="0" style="background-color:#f8fafc;padding:32px 16px;">
        <tr>
            <td align="center">
                <table role="presentation" width="100%" border="0" cellpadding="0" cellspacing="0" style="max-width:600px;background-color:#ffffff;border-radius:12px;border:1px solid #e2e8f0;overflow:hidden;box-shadow:0 1px 3px 0 rgba(0,0,0,0.05);">
                    <!-- Header -->
                    <tr>
                        <td style="padding:28px 32px;background-color:#0f172a;border-bottom:1px solid #1e293b;">
                            <table role="presentation" width="100%" border="0" cellpadding="0" cellspacing="0">
                                <tr>
                                    <td>
                                        <span style="font-size:20px;font-weight:700;color:#ffffff;letter-spacing:-0.5px;">ZenaTech</span>
                                        <span style="font-size:13px;font-weight:500;color:#94a3b8;margin-left:8px;">Compliance & Collaboration</span>
                                    </td>
                                </tr>
                            </table>
                        </td>
                    </tr>
                    <!-- Body -->
                    <tr>
                        <td style="padding:32px;">
                            <h1 style="margin:0 0 16px 0;font-size:20px;font-weight:700;color:#0f172a;line-height:1.4;">{title}</h1>
                            {body_html}
                            {action_btn_html}
                            <hr style="border:0;border-top:1px solid #e2e8f0;margin:32px 0 20px 0;">
                            <p style="margin:0;font-size:12px;color:#64748b;line-height:1.5;">
                                This is an automated notification from the ZenaTech Video Compliance & Collaboration Platform.
                            </p>
                        </td>
                    </tr>
                </table>
            </td>
        </tr>
    </table>
</body>
</html>"""


async def send_compliance_alert_email(
    to_emails: Union[str, List[str]],
    video_title: str,
    rule_name: str,
    severity: str,
    timestamp_seconds: float,
    details: str,
    dashboard_url: str,
) -> bool:
    """Notify reviewer/creator about an AI compliance flag."""
    badge_color = "#dc2626" if severity.upper() == "CRITICAL" else "#ea580c" if severity.upper() == "HIGH" else "#4f46e5"
    minutes = int(timestamp_seconds // 60)
    seconds = int(timestamp_seconds % 60)
    time_str = f"{minutes:02d}:{seconds:02d}"

    subject = f"[{severity.upper()}] Compliance Alert: {video_title}"
    preheader = f"Compliance issue detected in {video_title} at {time_str}"
    body_html = f"""
        <div style="background-color:#fef2f2;border-left:4px solid {badge_color};padding:16px;border-radius:4px;margin-bottom:20px;">
            <p style="margin:0 0 8px 0;font-size:14px;font-weight:600;color:#991b1b;">
                AI Rule Violation: {rule_name}
            </p>
            <p style="margin:0;font-size:13px;color:#7f1d1d;line-height:1.5;">
                {details}
            </p>
        </div>
        <p style="font-size:14px;color:#334155;margin:0 0 8px 0;"><strong>Video:</strong> {video_title}</p>
        <p style="font-size:14px;color:#334155;margin:0 0 8px 0;"><strong>Timestamp:</strong> {time_str}</p>
        <p style="font-size:14px;color:#334155;margin:0;"><strong>Severity:</strong> <span style="display:inline-block;padding:2px 8px;font-size:12px;font-weight:600;color:#ffffff;background-color:{badge_color};border-radius:9999px;">{severity.upper()}</span></p>
    """
    html = _render_base_email(
        title=f"Compliance Alert: {video_title}",
        preheader=preheader,
        body_html=body_html,
        action_url=dashboard_url,
        action_label="Review Video & Resolve Issue",
    )
    return await send_email(to_emails, subject, html)
