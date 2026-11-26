"""Preserve safe rich caption formatting during reviewer edits and legacy no-op saves."""
from html import escape
from services.compliance.media.html_sanitizer import sanitize_caption_html


def saved_html(text, html, previous_text="", previous_html=None):
    if html is not None:
        return sanitize_caption_html(html)
    if text == previous_text:
        return sanitize_caption_html(previous_html)
    return "<p>" + escape(text).replace("\n", "<br>") + "</p>"
