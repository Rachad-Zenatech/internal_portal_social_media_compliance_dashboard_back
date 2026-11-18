"""Lossless line/table conversion for scripts submitted through Create Post."""

from fastapi import HTTPException
from services.compliance.evaluation import shared_script_service as shared


def submission_beats(title, body):
    details, beats = {}, []
    lines = body.replace("\r\n", "\n").splitlines()
    first = True
    for line in lines:
        if not line.strip():
            continue
        if first and line.strip().lstrip("# ") == title:
            first = False
            continue
        first = False
        if line.strip().startswith("|") and len(line.strip().strip("|").split("|")) > 4:
            beats.append({"time": "", "visual": "", "voiceover": line.strip(), "onscreen": ""})
            continue
        metadata, rows = shared.text_to_beats(title + "\n" + line)
        details.update(metadata)
        beats.extend(rows)
    if len(beats) > 2000:
        raise HTTPException(422, "Split this upload into smaller script documents before submitting it.")
    if not beats and not details:
        raise HTTPException(422, "The submitted script is empty")
    return details, beats
