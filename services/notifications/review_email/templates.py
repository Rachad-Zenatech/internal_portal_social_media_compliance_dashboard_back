"""Short, accessible workflow emails using the admin dashboard's branded SendGrid shell."""
from html import escape
from services.notifications.email_service import _render_base_email
from services.notifications.review_email.config import portal_url, when
from services.notifications.review_email.policy import date_value


def item_payload(item) -> dict:
    return {"key": item.key, "title": item.title, "kind": item.kind, "reference": item.reference,
            "path": item.path, "due_at": item.due_at.isoformat() if item.due_at else None,
            "resubmitted": item.resubmitted}


def _item_html(item: dict, time_zone: str | None = None) -> str:
    url = escape(portal_url(item["path"]), quote=True)
    label = escape(f"{item['kind']} - {item['reference']}")
    due = escape(when(date_value(item.get("due_at")), time_zone))
    return (f'<li style="margin:0 0 16px;padding:14px;background:#f8fafc;border:1px solid #e2e8f0;border-radius:8px;">'
            f'<div style="font-size:12px;color:#64748b;">{label}</div><strong>{escape(item["title"])}</strong>'
            f'<div style="font-size:13px;margin:4px 0 10px;">Due: {due}</div>'
            f'<a href="{url}" style="color:#245a9e;font-weight:600;">Open {item["kind"].lower()} &rarr;</a></li>')


def _item_text(item: dict, time_zone: str | None = None) -> str:
    return f"{item['kind']} - {item['reference']}: {item['title']}\nDue: {when(date_value(item.get('due_at')), time_zone)}\n{portal_url(item['path'])}"


def render(kind: str, payload: dict, name: str, time_zone: str | None = None) -> tuple[str, str, str]:
    greeting = f"Hi {escape(name)},"
    items = payload.get("items", [])
    if kind == "DIGEST":
        subject = "Your morning review list"
        intro = "These posts and scripts arrived yesterday and are still waiting for your review."
        sections, plain = [], []
        for again, heading in ((False, "New submissions"), (True, "Updated and resubmitted")):
            group = [item for item in items if bool(item.get("resubmitted")) == again]
            content = '<ul style="list-style:none;padding:0;">' + "".join(_item_html(i, time_zone) for i in group) + "</ul>" if group else '<p style="color:#64748b;">None waiting.</p>'
            sections.append(f'<h2 style="font-size:16px;margin:24px 0 12px;">{heading} ({len(group)})</h2>{content}')
            plain.append(heading + "\n" + ("\n\n".join(_item_text(i, time_zone) for i in group) or "None waiting."))
        body = f"<p>{greeting}</p><p>{intro}</p>" + "".join(sections)
        text = f"Hi {name},\n{intro}\n\n" + "\n\n".join(plain)
        action, action_label = "/compliance/scanner", "Open your review queue"
    elif kind == "REMINDER":
        creator = payload["stage"] == "REVISION"
        subject = "Requested changes are due within one hour" if creator else "Your review is due within one hour"
        intro = "Please finish your changes and resubmit before the deadline." if creator else "This item is still waiting for your review. Please review it before the deadline."
        body = f"<p>{greeting}</p><p>{intro}</p><ul style='list-style:none;padding:0;'>" + "".join(_item_html(i, time_zone) for i in items) + "</ul>"
        text = f"Hi {name},\n{intro}\n\n" + "\n\n".join(_item_text(i, time_zone) for i in items)
        action, action_label = items[0]["path"], "Make changes" if creator else "Review now"
    else:
        subject, intro = payload["label"], payload["message"]
        if payload.get("changes_due_at"):
            intro += f"\nChanges due: {when(date_value(payload['changes_due_at']), time_zone)}. Resubmit before this deadline."
        body = (f"<p>{greeting}</p><p><strong>{escape(payload['kind'])} - {escape(payload['reference'])}: "
                f"{escape(payload['title'])}</strong></p><p style='white-space:pre-line;'>{escape(intro)}</p>")
        text = f"Hi {name},\n{subject}\n{payload['kind']} - {payload['reference']}: {payload['title']}\n{intro}\n{portal_url(payload['path'])}"
        action, action_label = payload["path"], "Open your script" if payload["kind"] == "Script" else "Open your post"
    html = _render_base_email(escape(subject), escape(subject), body, escape(portal_url(action), quote=True), action_label)
    return subject, html, text
