"""Script templates: a table of beats, each with what is seen, what is said and the text on screen. Only the blank
script is offered for now; add more with _t()."""

import re
from typing import Dict, List, Optional

from models.compliance.script_doc_model import ScriptTemplate, TemplateRow


def _t(key: str, name: str, storyline: str, description: str, duration: str, beats: List[tuple]) -> ScriptTemplate:
    return ScriptTemplate(
        key=key, name=name, storyline=storyline, description=description, duration=duration,
        rows=[TemplateRow(time=b[0], visual=b[1], voiceover=b[2], onscreen=b[3] if len(b) > 3 else "") for b in beats],
    )


TEMPLATES: List[ScriptTemplate] = [
    # Just an empty page. (Add script and uploaded scripts use the Reels AV script table, see empty_script_html.)
    _t("blank", "Blank script", "", "An empty page to write your script on.", "", []),
]

_BY_KEY: Dict[str, ScriptTemplate] = {t.key: t for t in TEMPLATES}


def get_template(key: Optional[str]) -> ScriptTemplate:
    return _BY_KEY.get(key or "blank") or _BY_KEY["blank"]


def _esc(s: str) -> str:
    return (s or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def template_html(t: ScriptTemplate, title: str) -> str:
    """The starting text of a script made from the template: for the blank script, an empty page."""
    if not t.rows:
        return "<p></p>"
    head = ("<tr><th><p>Time</p></th><th><p>Visual / footage</p></th><th><p>Voiceover</p></th>"
            "<th><p>On-screen text</p></th></tr>")
    body = "".join(
        f"<tr><td><p>{_esc(r.time)}</p></td><td><p>{_esc(r.visual)}</p></td><td><p>{_esc(r.voiceover)}</p></td>"
        f"<td><p>{_esc(r.onscreen)}</p></td></tr>"
        for r in t.rows
    )
    return (f"<h2>1. {_esc(title)}</h2><p><strong>Storyline:</strong> {_esc(t.storyline)}</p>"
            f"<p>Hook score 80.8 (strong) | {_esc(t.duration or 'about 35 seconds')} | Green lane</p>"
            f"<table><tbody>{head}{body}</tbody></table><p></p>")


def empty_script_html(n: int, title: str) -> str:
    """A new page: the same format as the template (numbered heading, storyline, facts line, table of beats), empty."""
    row = "<tr><td><p></p></td><td><p></p></td><td><p></p></td><td><p></p></td></tr>"
    head = ("<tr><th><p>Time</p></th><th><p>Visual / footage</p></th><th><p>Voiceover</p></th>"
            "<th><p>On-screen text</p></th></tr>")
    return (f"<h2>{n}. {_esc(title)}</h2><p><strong>Storyline:</strong> </p><p>Hook score | about 35 seconds | Green lane</p>"
            f"<table><tbody>{head}{row * 5}</tbody></table><p></p>")


_MARKS = re.compile(r"\*\*|(?<![\w])_(?=\S)|(?<=\S)_(?![\w])")


def text_to_html(title: str, body: str) -> str:
    """The editor's page for an uploaded script's text (as read from the file: "| a | b |" table rows, "# " headings)."""
    out = [f"<h2>{_esc(title)}</h2>"]
    lines = (body or "").replace("\r\n", "\n").split("\n")
    i = 0
    while i < len(lines):
        line = lines[i].strip()
        if not line or re.match(r"^--- Page \d+ ---$", line):
            i += 1
        elif re.match(r"^\|.*\|$", line):
            rows = []
            while i < len(lines) and re.match(r"^\|.*\|$", lines[i].strip()):
                cells = [c.strip() for c in lines[i].strip().strip("|").split("|")]
                if not all(re.fullmatch(r":?-{2,}:?", c) for c in cells):
                    rows.append(cells)
                i += 1
            width = max((len(r) for r in rows), default=1)
            html_rows = []
            for n, r in enumerate(rows):
                tag = "th" if n == 0 else "td"
                cells = r + [""] * (width - len(r))
                html_rows.append("<tr>" + "".join(f"<{tag}><p>{_esc(_MARKS.sub('', c))}</p></{tag}>" for c in cells) + "</tr>")
            out.append(f"<table><tbody>{''.join(html_rows)}</tbody></table>")
        else:
            heading = re.match(r"^(#{1,3}) (.*)$", line)
            if heading:
                out.append(f"<h3>{_esc(_MARKS.sub('', heading.group(2)))}</h3>")
            else:
                out.append(f"<p>{_esc(_MARKS.sub('', line))}</p>")
            i += 1
    return "".join(out) + "<p></p>"
