"""One durable claim inventory per exact published revision, shared by every source reader."""
import asyncio
import hashlib
import json
import logging
import os
import re
import tempfile
from pathlib import Path
from models.compliance.claim_manifest_model import ClaimManifest, EvidenceClaim, ClaimLocation
from . import claim_discovery, claim_payload
from .image_marks import IMAGE_MARK

VERSION = "claims-1"
ROLES = {"factual", "diagram", "opinion", "promotional", "identifier", "disclosure", "citation", "ambiguous"}
_locks = [asyncio.Lock() for _ in range(32)]
logger = logging.getLogger(__name__)


def identity(text):
    return hashlib.sha256(" ".join(text.casefold().split()).encode()).hexdigest()[:24]


def passages(text):
    """Keep original offsets and whole lines; do not split U.S., decimals or OCR-wrapped negations."""
    out, where, image = [], "caption", None
    for match in re.finditer(r"[^\r\n]+", text):
        line, start = match.group(), match.start()
        marker = IMAGE_MARK.search(line)
        if marker:
            where = "image"
            image = int(re.search(r"\d+", marker.group()).group())
            start += marker.end()
            line = line[marker.end():]
        body = line.strip()
        if body:
            start += len(line) - len(line.lstrip())
            out.append({"index": len(out), "wording": body, "start": start, "end": start + len(body),
                        "where": where, "image": image})
    return out


def _claim(item, passage, text):
    wording = item.get("wording", "")
    if not isinstance(wording, str) or not wording.strip():
        return None
    wording = wording.strip()
    # Cross-line quotes may be proposed for OCR wraps; they must still be verbatim in the complete post.
    offset = text.find(wording, passage["start"])
    if offset < 0 or offset >= passage["end"]:
        return None
    before = text[passage["start"]:offset]
    after = text[offset + len(wording):passage["end"]]
    # A model may select an attractive fragment. Preserve a local negation or limiting clause in its quote.
    expanded = bool(re.search(r"\b(?:no|not|never|without|unless|only|if|may|might|could|planned|target)\b", before + " " + after, re.I))
    if expanded:
        wording, offset = passage["wording"], passage["start"]
    fields = {key: str(item.get(key) or "") for key in
              ("subject", "assertion", "scope", "qualification", "evidence_needed")}
    attribution = item.get("attribution", "ambiguous")
    attribution = attribution if isinstance(attribution, str) and attribution in {"explicit", "inferred", "ambiguous"} else "ambiguous"
    ambiguous = expanded or bool(item.get("ambiguous")) or attribution == "ambiguous" or not all(
        fields[key] for key in ("subject", "assertion", "evidence_needed"))
    key = "|".join([wording, fields["subject"], fields["assertion"], fields["scope"]])
    return EvidenceClaim(id=identity(key), wording=wording, **fields, attribution=attribution,
        ambiguous=ambiguous, locations=[ClaimLocation(start=offset, end=offset + len(wording),
            where=passage["where"], image=passage["image"])])


def assemble(text, units, answers):
    answers = answers or []
    indexed = {a.get("index"): a for a in answers if isinstance(a, dict) and isinstance(a.get("index"), int)}
    claims, coverage, complete = {}, [], True
    for unit in units:
        covered = any(loc.start <= unit["start"] and loc.end >= unit["end"]
                      for claim in claims.values() for loc in claim.locations)
        answer = indexed.get(unit["index"], {})
        role = answer.get("role", "ambiguous")
        valid = isinstance(role, str) and role in ROLES and isinstance(answer.get("claims"), list)
        if not valid:
            complete, role = complete and covered, "factual" if covered else "ambiguous"
        raw = answer.get("claims", []) if valid else []
        accepted = [_claim(c, unit, text) for c in raw if isinstance(c, dict)]
        if any(c is None for c in accepted):
            complete = False
        accepted = [c for c in accepted if c]
        if role in {"opinion", "promotional", "identifier"} and not accepted and claim_discovery.potential_fact(unit["wording"]):
            role = "ambiguous"  # catch softer facts accidentally classified as promotional copy; never deduct on this hint
        if role in {"factual", "diagram", "ambiguous"} and not accepted and not covered:
            # Missing discovery coverage must remain visible, never become a silent pass or a deduction.
            accepted = [_claim({"wording": unit["wording"], "ambiguous": True}, unit, text)]
        coverage.append({**unit, "role": role})
        for claim in accepted:
            if claim.id in claims:
                old = claims[claim.id]
                old.locations.extend(loc for loc in claim.locations if loc not in old.locations)
                old.ambiguous = old.ambiguous or claim.ambiguous
            else:
                claims[claim.id] = claim
    return ClaimManifest(version=VERSION, revision=hashlib.sha256(text.encode()).hexdigest(),
        complete=complete, published_text=text, claims=list(claims.values()), passages=coverage)


def _path(revision):
    root = Path(os.getenv("CLAIM_MANIFEST_DIR", str(Path(__file__).resolve().parents[3] / ".data" / "claim_manifests")))
    return root / (VERSION + "-" + revision + ".json")


def _load(path, revision):
    try:
        manifest = ClaimManifest.model_validate_json(path.read_text(encoding="utf-8"))
        if manifest.complete and manifest.version == VERSION and manifest.revision == revision:
            return manifest
    except (OSError, ValueError):
        pass
    return None


def _save(path, manifest):
    temporary = None
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent, delete=False) as stream:
            temporary = Path(stream.name)
            stream.write(manifest.model_dump_json())
        try:
            os.link(temporary, path)  # first completed inventory wins, including across worker processes
        except FileExistsError:
            if _load(path, manifest.revision) is None:
                os.replace(temporary, path)
    except OSError:
        logger.warning("Claim inventory could not be persisted; this check will be marked incomplete")
        manifest.complete = False
    finally:
        if temporary and temporary.exists():
            temporary.unlink()


async def get(text):
    revision = hashlib.sha256(text.encode()).hexdigest()
    path = _path(revision)
    async with _locks[int(revision[:4], 16) % len(_locks)]:
        saved = _load(path, revision)
        if saved:
            return saved
        units, answers = passages(text), []
        for group in claim_payload.batches(units, max_items=30):
            batch = await claim_discovery.classify(text, group)
            answers.extend(batch or [])
        manifest = assemble(text, units, answers)
        if manifest.complete:
            _save(path, manifest)
        return _load(path, revision) or manifest
