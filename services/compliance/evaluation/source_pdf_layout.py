"""Preserve PDF pages, repeated values and aligned cells without inventing table contents."""
import re

_PAGE = re.compile(r"^--- Page (\d+) ---$", re.MULTILINE)


def clean(text):
    return "\n".join(line.rstrip() for line in text.replace("\r\n", "\n").replace("\xa0", " ").expandtabs(4).splitlines()).strip()


def has_text(text, minimum):
    body = _PAGE.sub("", text)
    return sum(char.isalnum() for char in body) >= minimum


def readable(text):
    body = _PAGE.sub("", text)
    if not has_text(body, 40) or len(re.findall(r"[^\W\d_]{2,}", body)) < 3:
        return False
    if re.search(r"\(cid:\d+\)", body) or "\x00" in body:
        return False
    return body.count("\ufffd") <= max(1, len(body) // 200)


def native_pages(text):
    marks = list(_PAGE.finditer(text))
    if not marks:
        return [text] if text.strip() else []
    if [int(mark.group(1)) for mark in marks] != list(range(1, len(marks) + 1)):
        return []
    return [text[mark.end():marks[i + 1].start() if i + 1 < len(marks) else len(text)].strip()
            for i, mark in enumerate(marks)]


def _table(block):
    width = max(map(len, block))
    # Only split at gutters shared by every row; right-aligned values remain in their original columns.
    mask = "".join(" " if all(i >= len(row) or row[i].isspace() for row in block) else "x" for i in range(width))
    gaps = [match for match in re.finditer(r" {2,}", mask) if match.start() > 0 and match.end() < width]
    if not gaps:
        return block
    starts, ends = [0] + [gap.end() for gap in gaps], [gap.start() for gap in gaps] + [width]
    return ["| " + " | ".join(" ".join(row[a:b].split()).replace("|", r"\|") for a, b in zip(starts, ends)) + " |"
            for row in block]


def format_page(text):
    lines, output, block, spacing = clean(text).splitlines(), [], [], []
    for index, line in enumerate(lines + [""]):
        cells = re.split(r" {2,}", line.strip())
        if len(cells) > 1 and not line.lstrip().startswith("|"):
            block.append(line)
            spacing = []
            continue
        if not line.strip() and block:
            spacing.append(line)
            continue
        if block and line.strip():
            indent = len(line) - len(line.lstrip())
            first = min(len(row) - len(row.lstrip()) for row in block)
            following = next((row for row in lines[index + 1:] if row.strip()), "")
            if indent > first + 2 and len(re.split(r" {2,}", following.strip())) > 1:
                block.append(line)  # Keep a wrapped cell in its original column, with empty neighbours.
                continue
        if block:
            output.extend(_table(block) if len(block) >= 2 else block)
            output.extend(spacing)
            block, spacing = [], []
        output.append(line)
    if block:
        output.extend(_table(block) if len(block) >= 2 else block)
    return clean("\n".join(output))


def join_pages(pages):
    return "\n\n".join(f"--- Page {number} ---\n{text}" for number, text in enumerate(pages, start=1))
