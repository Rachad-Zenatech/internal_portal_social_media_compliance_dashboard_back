"""Brand voice per brand, read from its brand manual.

A reviewer (or a rulebook editor) uploads a brand's manual as a PDF. It's read in the background:
- straight from the PDF: the colour codes it prints, the fonts it's set in and the images it holds (logo candidates);
- by Gemini: the voice and tone, do's and don'ts, the named colour palette, the typefaces and which image is the logo.
When the AI can't run (quota, outage), what the PDF itself gives is kept (status PARTIAL) and the rest can be filled in
by hand. Every field stays editable.

The result is kept as the brand's Brand Voice entry in the Messaging Context (auto_source BRAND_MANUAL), so the content
analysis and caption suggestions use it like any other brand voice entry.
"""

import asyncio
import json
import logging
import os
import re
from collections import Counter
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple
from uuid import UUID

from fastapi import HTTPException, UploadFile
from google import genai
from google.genai import types
from pydantic import BaseModel, Field

from models.compliance.brand_voice_model import BrandColor, BrandTypeface, BrandVoice, BrandVoiceIn, PaletteRow
from postgresql_db.database import fetch_all, fetch_one
from services.shared.core.audit_service import log_audit_event

logger = logging.getLogger(__name__)

STORAGE_ROOT = Path(os.getenv("BRAND_VOICE_STORAGE_DIR", ".data/brand_voice")).resolve()
MAX_MANUAL_BYTES = int(os.getenv("BRAND_MANUAL_MAX_MB", "40")) * 1024 * 1024
MAX_TEXT_CHARS = 60_000
MAX_LOGO_CANDIDATES = 8
# Pages are also shown to the AI as images (logos and swatches are often drawn as vector shapes, not image files):
# each page in sections about as tall as an A4 page, so a long single-page manual isn't shrunk to an unreadable strip.
SECTION_WIDTH_PX = 1000
MAX_SECTIONS = 16
STALE_PROCESSING_MIN = 15
AUTO_SOURCE = "BRAND_MANUAL"

_tasks: Set[asyncio.Task] = set()
_client: Optional[genai.Client] = None


# --------------------------------------------------------------------------------------------------------------------
# Access
# --------------------------------------------------------------------------------------------------------------------

async def _may_edit(user_id: UUID, brand_code: str) -> bool:
    """Super admins, rulebook editors, and reviewers for the brands they work on."""
    from services.compliance.submissions import submission_service as subs
    from services.shared.auth.auth_service import user_can_access_page

    if await subs._is_super_admin(user_id) or await user_can_access_page(user_id, "CONFIG_ROLES", "UPDATE"):
        return True
    if not await subs._can_review(user_id):
        return False
    scope = await subs._brand_scope(user_id)
    return scope is None or brand_code in scope


async def _require_edit(user_id: UUID, brand_code: str) -> Dict[str, Any]:
    brand = await fetch_one("SELECT code, name FROM brands WHERE code = $1 AND is_active", brand_code)
    if not brand:
        raise HTTPException(status_code=404, detail="Brand not found")
    if not await _may_edit(user_id, brand_code):
        raise HTTPException(status_code=403, detail="Only reviewers for this brand (or rulebook editors) can change its voice")
    return dict(brand)


# --------------------------------------------------------------------------------------------------------------------
# Reading back
# --------------------------------------------------------------------------------------------------------------------

_SELECT = """
    -- The profile's own columns first, so a brand with no profile yet keeps its code (p.brand_code is NULL then).
    SELECT p.*, b.code AS brand_code, b.name AS brand_name, u.full_name AS updated_by_name
    FROM brands b
    LEFT JOIN brand_voice_profiles p ON p.brand_code = b.code
    LEFT JOIN users u ON u.id = p.updated_by
"""


def _json_list(value: Any) -> List[Any]:
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except ValueError:
            return []
    return list(value or [])


def _out(r: Dict[str, Any], can_edit: bool) -> BrandVoice:
    status = r.get("status") or "NONE"
    error = r.get("error")
    started = r.get("processing_started_at")
    if status == "PROCESSING" and started is not None:
        from datetime import datetime, timedelta, timezone
        if datetime.now(timezone.utc) - started > timedelta(minutes=STALE_PROCESSING_MIN):
            status, error = "FAILED", "Reading the manual stopped part way. Read it again."
    return BrandVoice(
        brand_code=r["brand_code"], brand_name=r["brand_name"], status=status, error=error,
        voice=r.get("voice"), tone=list(r.get("tone") or []),
        do_examples=list(r.get("do_examples") or []), dont_examples=list(r.get("dont_examples") or []),
        colors=[BrandColor(**c) for c in _json_list(r.get("colors"))],
        typefaces=[BrandTypeface(**t) for t in _json_list(r.get("typefaces"))],
        palette=[PaletteRow(**p) for p in _json_list(r.get("palette"))],
        logo_notes=r.get("logo_notes"),
        manual_name=r.get("manual_name"), manual_size=r.get("manual_size"),
        manual_uploaded_at=r.get("manual_uploaded_at"), has_logo=bool(r.get("logo_path")),
        extracted_at=r.get("extracted_at"), updated_at=r.get("updated_at"), updated_by_name=r.get("updated_by_name"),
        can_edit=can_edit,
    )


async def list_voices(user_id: UUID) -> List[BrandVoice]:
    """Every active brand with its voice (an empty one when no manual or voice was added yet)."""
    rows = await fetch_all(_SELECT + " WHERE b.is_active ORDER BY b.sort_order, b.name")
    return [_out(dict(r), await _may_edit(user_id, r["brand_code"])) for r in rows]


async def get_voice(brand_code: str, user_id: UUID) -> BrandVoice:
    row = await fetch_one(_SELECT + " WHERE b.code = $1", brand_code)
    if not row:
        raise HTTPException(status_code=404, detail="Brand not found")
    return _out(dict(row), await _may_edit(user_id, brand_code))


async def file_path(brand_code: str, which: str) -> Tuple[Path, str, str]:
    """The stored manual or logo for download: (path, media type, file name)."""
    row = await fetch_one("SELECT * FROM brand_voice_profiles WHERE brand_code = $1", brand_code)
    rel = row and (row["manual_path"] if which == "manual" else row["logo_path"])
    if not rel:
        raise HTTPException(status_code=404, detail="Not found")
    path = (STORAGE_ROOT / rel).resolve()
    if STORAGE_ROOT not in path.parents or not path.is_file():
        raise HTTPException(status_code=404, detail="Not found")
    if which == "manual":
        return path, "application/pdf", row["manual_name"] or f"{brand_code}-brand-manual.pdf"
    return path, row["logo_mime"] or "image/png", f"{brand_code}-logo{path.suffix}"


# --------------------------------------------------------------------------------------------------------------------
# Upload and edit
# --------------------------------------------------------------------------------------------------------------------

async def upload_manual(brand_code: str, upload: UploadFile, user_id: UUID) -> BrandVoice:
    await _require_edit(user_id, brand_code)
    name = Path(upload.filename or "brand-manual.pdf").name
    data = await upload.read()
    if not data:
        raise HTTPException(status_code=422, detail="The file is empty")
    if len(data) > MAX_MANUAL_BYTES:
        raise HTTPException(status_code=413, detail=f"The manual is over {MAX_MANUAL_BYTES // (1024 * 1024)} MB")
    if not data.startswith(b"%PDF"):
        raise HTTPException(status_code=422, detail="Upload the brand manual as a PDF")
    folder = STORAGE_ROOT / brand_code
    folder.mkdir(parents=True, exist_ok=True)
    for old in folder.glob("*"):
        old.unlink(missing_ok=True)
    path = folder / "manual.pdf"
    path.write_bytes(data)
    await fetch_one(
        """
        INSERT INTO brand_voice_profiles (brand_code, manual_name, manual_path, manual_size, manual_uploaded_at,
                                          manual_uploaded_by, status, error, processing_started_at, logo_path, logo_mime,
                                          updated_at, updated_by)
        VALUES ($1, $2, $3, $4, NOW(), $5, 'PROCESSING', NULL, NOW(), NULL, NULL, NOW(), $5)
        ON CONFLICT (brand_code) DO UPDATE SET
            manual_name = EXCLUDED.manual_name, manual_path = EXCLUDED.manual_path, manual_size = EXCLUDED.manual_size,
            manual_uploaded_at = NOW(), manual_uploaded_by = EXCLUDED.manual_uploaded_by, status = 'PROCESSING',
            error = NULL, processing_started_at = NOW(), logo_path = NULL, logo_mime = NULL,
            updated_at = NOW(), updated_by = EXCLUDED.updated_by
        RETURNING brand_code
        """,
        brand_code, name, str(path.relative_to(STORAGE_ROOT)), len(data), user_id,
    )
    await log_audit_event("brand_voice", brand_code, "UPLOAD_BRAND_MANUAL", user_id, {"file": name, "size": len(data)})
    schedule(brand_code)
    return await get_voice(brand_code, user_id)


async def reread(brand_code: str, user_id: UUID) -> BrandVoice:
    await _require_edit(user_id, brand_code)
    row = await fetch_one("SELECT manual_path FROM brand_voice_profiles WHERE brand_code = $1", brand_code)
    if not row or not row["manual_path"]:
        raise HTTPException(status_code=409, detail="Upload the brand manual first")
    await fetch_one(
        "UPDATE brand_voice_profiles SET status = 'PROCESSING', error = NULL, processing_started_at = NOW() "
        "WHERE brand_code = $1 RETURNING brand_code", brand_code,
    )
    schedule(brand_code)
    return await get_voice(brand_code, user_id)


async def update_voice(brand_code: str, data: BrandVoiceIn, user_id: UUID) -> BrandVoice:
    brand = await _require_edit(user_id, brand_code)
    clean = _clean(data)
    await fetch_one(
        """
        INSERT INTO brand_voice_profiles (brand_code, voice, tone, do_examples, dont_examples, colors, typefaces,
                                          logo_notes, status, updated_at, updated_by)
        VALUES ($1, $2, $3, $4, $5, $6::jsonb, $7::jsonb, $8, 'NONE', NOW(), $9)
        ON CONFLICT (brand_code) DO UPDATE SET
            voice = EXCLUDED.voice, tone = EXCLUDED.tone, do_examples = EXCLUDED.do_examples,
            dont_examples = EXCLUDED.dont_examples, colors = EXCLUDED.colors, typefaces = EXCLUDED.typefaces,
            logo_notes = EXCLUDED.logo_notes, updated_at = NOW(), updated_by = EXCLUDED.updated_by
        RETURNING brand_code
        """,
        brand_code, clean.voice, clean.tone, clean.do_examples, clean.dont_examples,
        json.dumps([c.model_dump() for c in clean.colors]), json.dumps([t.model_dump() for t in clean.typefaces]),
        clean.logo_notes, user_id,
    )
    await log_audit_event("brand_voice", brand_code, "UPDATE_BRAND_VOICE", user_id, clean.model_dump(mode="json"))
    await _sync_context_entry(brand_code, brand["name"], user_id)
    return await get_voice(brand_code, user_id)


def _clean(data: BrandVoiceIn) -> BrandVoiceIn:
    def items(values: List[str]) -> List[str]:
        return list(dict.fromkeys(v.strip() for v in values if v and v.strip()))

    colors, seen = [], set()
    for c in data.colors:
        key = c.hex.upper()
        if key not in seen:
            seen.add(key)
            colors.append(BrandColor(name=(c.name or "").strip() or None, hex=key, usage=(c.usage or "").strip() or None))
    return BrandVoiceIn(
        voice=(data.voice or "").strip() or None, tone=items(data.tone), do_examples=items(data.do_examples),
        dont_examples=items(data.dont_examples), colors=colors,
        typefaces=[BrandTypeface(name=t.name.strip(), role=(t.role or "").strip() or None, usage=(t.usage or "").strip() or None)
                   for t in data.typefaces if t.name.strip()],
        logo_notes=(data.logo_notes or "").strip() or None,
    )


# --------------------------------------------------------------------------------------------------------------------
# Reading the manual
# --------------------------------------------------------------------------------------------------------------------

_HEX_LABELLED = re.compile(r"(?:#|hex[:\s]*|HEX[:\s]*)([0-9A-Fa-f]{6})\b")
_SUBSET = re.compile(r"^[A-Z]{6}\+")
_FONT_STYLE = re.compile(r"[-,]?(Regular|Bold|Italic|Light|Medium|Semi[Bb]old|Black|Thin|Heavy|Book|Oblique|Condensed|"
                         r"ExtraBold|ExtraLight|BoldItalic|MT|PS)+$")


class PdfFacts(BaseModel):
    text: str = ""
    hexes: List[str] = Field(default_factory=list)
    fonts: List[str] = Field(default_factory=list)
    # (page number, width, height, bytes, extension) of each logo candidate, in document order.
    images: List[Tuple[int, int, int, bytes, str]] = Field(default_factory=list)
    # (page index, x0, y0, x1, y1, PNG) of each page section rendered for the AI, in reading order.
    sections: List[Tuple[int, float, float, float, float, bytes]] = Field(default_factory=list)
    # Rows of colour swatches drawn in the manual: (label, [#RRGGBB, ...]) in reading order.
    swatch_rows: List[Tuple[str, List[str]]] = Field(default_factory=list)


_ROW_LABELS = [("brighten", "Tints"), ("tint", "Tints"), ("lighten", "Tints"), ("darken", "Shades"), ("shade", "Shades"),
               ("monochrom", "Monochromatic"), ("complement", "Complementary"), ("analog", "Analogous"),
               ("triad", "Triadic"), ("gradient", "Gradient")]


def _swatch_rows(page) -> List[Tuple[str, List[str]]]:
    """Rows of three or more touching, same-height filled blocks (a palette strip), with the label above each."""
    blocks = []
    for d in page.get_drawings():
        fill = d.get("fill")
        r = d.get("rect")
        if not fill or r is None or len(d.get("items") or []) > 6:
            continue
        if not (15 <= r.height <= 200 and r.width >= 15):
            continue
        hexv = "#%02X%02X%02X" % tuple(max(0, min(255, round(c * 255))) for c in fill[:3])
        blocks.append((round(r.y0), round(r.height), r.x0, r.x1, hexv))
    by_line: Dict[Tuple[int, int], List[Tuple[float, float, str]]] = {}
    for y0, h, x0, x1, hexv in blocks:
        by_line.setdefault((y0, h), []).append((x0, x1, hexv))
    text = [(b[1], b[3], b[4].strip()) for b in page.get_text("blocks") if b[4].strip()]
    rows = []
    for (y0, h), items in sorted(by_line.items()):
        items.sort()
        run = [items[0]]
        runs = []
        for it in items[1:]:
            if it[0] - run[-1][1] <= 6:
                run.append(it)
            else:
                runs.append(run)
                run = [it]
        runs.append(run)
        for run in runs:
            hexes = [h_ for _, _, h_ in run]
            if len(run) < 3 or len(set(hexes)) < 3:
                continue
            above = [(y0 - bottom, t) for top, bottom, t in text if 0 <= y0 - bottom <= 140]
            label = None
            for _, t in sorted(above):
                low = t.lower()
                label = next((name for key, name in _ROW_LABELS if key in low), None)
                if label:
                    break
            rows.append((label or "Palette", hexes))
    return rows


def _sections(doc) -> List[Tuple[int, float, float, float, float, bytes]]:
    """Each page rendered as images about as tall as an A4 page, overlapping a little so nothing is cut in half."""
    import pymupdf as fitz

    out: List[Tuple[int, float, float, float, float, bytes]] = []
    for page in doc:
        r = page.rect
        window = r.width * 1.45
        zoom = min(SECTION_WIDTH_PX / r.width, 2.0)
        y = r.y0
        while y < r.y1 and len(out) < MAX_SECTIONS:
            end = y + window
            if r.y1 - end < window * 0.3:
                end = r.y1
            clip = fitz.Rect(r.x0, y, r.x1, end)
            pix = page.get_pixmap(clip=clip, matrix=fitz.Matrix(zoom, zoom), alpha=False)
            out.append((page.number, clip.x0, clip.y0, clip.x1, clip.y1, pix.tobytes("png")))
            if end >= r.y1:
                break
            y = end - window * 0.15
        if len(out) >= MAX_SECTIONS:
            break
    return out


def crop_logo(data: bytes, section: Tuple[int, float, float, float, float, bytes], box: List[int]) -> Optional[bytes]:
    """Cut the logo out of the page at high resolution, from a box [ymin, xmin, ymax, xmax] (0-1000) in a section."""
    import pymupdf as fitz

    if len(box) != 4:
        return None
    ymin, xmin, ymax, xmax = (max(0, min(1000, int(v))) for v in box)
    if ymax - ymin < 5 or xmax - xmin < 5:
        return None
    page_no, x0, y0, x1, y1, _ = section
    w, h = x1 - x0, y1 - y0
    pad_x, pad_y = (xmax - xmin) * w / 1000 * 0.08, (ymax - ymin) * h / 1000 * 0.08
    doc = fitz.open(stream=data, filetype="pdf")
    try:
        page = doc[page_no]
        clip = fitz.Rect(x0 + xmin * w / 1000 - pad_x, y0 + ymin * h / 1000 - pad_y,
                         x0 + xmax * w / 1000 + pad_x, y0 + ymax * h / 1000 + pad_y) & page.rect
        if clip.is_empty or clip.width < 4 or clip.height < 4:
            return None
        zoom = max(1.0, min(4.0, 800 / clip.width))
        return page.get_pixmap(clip=clip, matrix=fitz.Matrix(zoom, zoom), alpha=False).tobytes("png")
    finally:
        doc.close()


def read_pdf(data: bytes) -> PdfFacts:
    """What the PDF gives without AI: its text, the colour codes it prints, its fonts and its images."""
    import pymupdf as fitz

    doc = fitz.open(stream=data, filetype="pdf")
    try:
        text_parts, fonts = [], Counter()
        for page in doc:
            if sum(len(t) for t in text_parts) < MAX_TEXT_CHARS:
                text_parts.append(f"[Page {page.number + 1}]\n{page.get_text()}")
            for f in page.get_fonts(full=False):
                family = _FONT_STYLE.sub("", _SUBSET.sub("", f[3] or "")).replace("-", " ").strip()
                if family and not family.lower().startswith(("symbol", "zapf", "wingding")):
                    fonts[family] += 1
        text = "\n".join(text_parts)[:MAX_TEXT_CHARS]
        labelled = [h.upper() for h in _HEX_LABELLED.findall(text)]
        hexes = list(dict.fromkeys(f"#{h}" for h in labelled))[:24]
        images, seen = [], set()
        for page in doc:
            if page.number >= 15 or len(images) >= MAX_LOGO_CANDIDATES:
                break
            for img in page.get_images(full=True):
                xref = img[0]
                if xref in seen:
                    continue
                seen.add(xref)
                try:
                    info = doc.extract_image(xref)
                except Exception:
                    continue
                w, h = info.get("width", 0), info.get("height", 0)
                if w < 48 or h < 48 or len(info.get("image", b"")) > 4 * 1024 * 1024:
                    continue
                images.append((page.number + 1, w, h, info["image"], info.get("ext", "png")))
                if len(images) >= MAX_LOGO_CANDIDATES:
                    break
        swatches = []
        for page in doc:
            if page.number >= 40:
                break
            swatches += _swatch_rows(page)
        return PdfFacts(text=text, hexes=hexes, fonts=[f for f, _ in fonts.most_common(6)], images=images,
                        sections=_sections(doc), swatch_rows=swatches[:60])
    finally:
        doc.close()


class _AiColor(BaseModel):
    name: str = ""
    hex: str = ""
    usage: str = ""


class _AiTypeface(BaseModel):
    name: str = ""
    role: str = ""
    usage: str = ""


class _AiResult(BaseModel):
    voice: str = Field("", description="The brand's voice and personality in 2-5 sentences, as guidance for writers.")
    tone: List[str] = Field(default_factory=list, description="3-8 tone words, e.g. confident, plain-spoken.")
    do_examples: List[str] = Field(default_factory=list, description="3-6 short do's for posts in this brand.")
    dont_examples: List[str] = Field(default_factory=list, description="3-6 short don'ts for posts in this brand.")
    colors: List[_AiColor] = Field(default_factory=list, description="The brand palette: name, #RRGGBB hex, usage.")
    typefaces: List[_AiTypeface] = Field(default_factory=list, description="Typefaces: family name, role, usage.")
    logo_section: int = Field(-1, description="The PAGE SECTION number showing the primary logo, or -1.")
    logo_box: List[int] = Field(default_factory=list,
                                description="Tight box around that logo in the section: [ymin, xmin, ymax, xmax], 0-1000.")
    logo_image: int = Field(-1, description="If the logo is one of the numbered IMAGES instead, its number; else -1.")
    logo_notes: str = Field("", description="Logo guidance: versions and backgrounds, clear space, minimum size, misuse.")


PROMPT = """You are reading a company's brand manual (brand guidelines): its text, the PAGE SECTIONS (the pages
rendered as images, top to bottom) and any embedded IMAGES. Fill in every field:
- voice: the brand's voice and personality, as 2-5 sentences of guidance for someone writing social media posts.
  Use the manual's own words where it has them; otherwise draw it from what it says about the brand, its colours
  and its type (the feelings and qualities it describes).
- tone: 3-8 tone words.
- do_examples / dont_examples: 3-6 short, practical do's and don'ts each for social posts in this brand. Take the
  manual's rules where it states them; otherwise derive them from its guidance (voice, colour meanings and usage,
  typefaces, logo versions), e.g. "Use Ottoman Red sparingly, to draw attention" or "Don't recolour the logo".
- colors: every brand colour, each with its name, its hex code as #RRGGBB (convert RGB or CMYK if only those are
  given) and its usage: what it's for (primary, accent, text, backgrounds...) and the feeling it carries, from the
  manual's description of it.
- typefaces: each typeface family with its role (e.g. Primary, Headings, Body) and usage. Correct obvious misspellings
  of well-known families (e.g. "Montserrate" is Montserrat).
- the primary logo: logos are often drawn as shapes, so look at the PAGE SECTIONS. Give logo_section (the section
  number) and logo_box, a tight box [ymin, xmin, ymax, xmax] on a 0-1000 scale within that section image, around the
  full logo (symbol and wordmark) and nothing else. Prefer the full-colour version on a white or light background
  when there are several. If the logo is instead one of the numbered IMAGES, give logo_image.
- logo_notes: the logo rules it states (clear space, minimum size, misuse); and the versions it shows and the
  backgrounds each is for (e.g. "white on dark backgrounds, blue on white, charcoal on light grey").
Base everything on this manual. Don't make up facts about the company."""


def _get_client() -> genai.Client:
    global _client
    if _client is None:
        api_key = os.getenv("GEMINI_API_KEY")
        if not api_key:
            raise RuntimeError("GEMINI_API_KEY is not set")
        _client = genai.Client(api_key=api_key)
    return _client


async def _ask_ai(data: bytes, facts: PdfFacts) -> _AiResult:
    from services.ai.core.gemini_service import generate_content_with_fallback

    contents: List[Any] = ["MANUAL TEXT:\n" + (facts.text or "(none: the manual is all images)")]
    for n, (page_no, _, y0, _, y1, png) in enumerate(facts.sections):
        contents += [f"PAGE SECTION {n} (page {page_no + 1}, {y0:.0f}-{y1:.0f} pt from the top):",
                     types.Part.from_bytes(data=png, mime_type="image/png")]
    for n, (page, w, h, image, ext) in enumerate(facts.images):
        mime = "image/jpeg" if ext in ("jpg", "jpeg") else f"image/{ext}"
        contents += [f"IMAGE {n} (page {page}, {w}x{h}):", types.Part.from_bytes(data=image, mime_type=mime)]
    contents.append(PROMPT)
    config = types.GenerateContentConfig(response_mime_type="application/json", response_schema=_AiResult, temperature=0)
    response = await generate_content_with_fallback(_get_client(), contents, config, feature="BRAND_VOICE")
    parsed = getattr(response, "parsed", None)
    if isinstance(parsed, _AiResult):
        return parsed
    return _AiResult.model_validate_json(response.text or "{}")


def _hex(value: str) -> Optional[str]:
    m = re.fullmatch(r"#?([0-9A-Fa-f]{6})", (value or "").strip())
    return f"#{m.group(1).upper()}" if m else None


def _logo_guess(facts: PdfFacts) -> Optional[int]:
    """Without AI: the biggest image on the earliest page that has one (manuals open with the logo)."""
    if not facts.images:
        return None
    first = facts.images[0][0]
    on_first = [(i, w * h) for i, (p, w, h, _, _) in enumerate(facts.images) if p == first]
    return max(on_first, key=lambda x: x[1])[0]


def _rgb(hexv: str) -> Tuple[int, int, int]:
    return int(hexv[1:3], 16), int(hexv[3:5], 16), int(hexv[5:7], 16)


def build_palette(rows: List[Tuple[str, List[str]]], main: List[str]) -> List[PaletteRow]:
    """Each swatch row under its main colour: the one it starts with, else the closest main colour, else its own
    first swatch. A row of only main colours (the palette overview) is left out; they're already the colours."""
    main = [m.upper() for m in main]
    out: List[PaletteRow] = []
    for label, hexes in rows:
        if set(hexes) <= set(main):
            continue
        first = hexes[0].upper()
        if first in main or not main:
            base = first
        else:
            base = min(main, key=lambda m: sum((a - b) ** 2 for a, b in zip(_rgb(m), _rgb(first))))
        out.append(PaletteRow(base_hex=base, label=label, colors=list(dict.fromkeys(h.upper() for h in hexes))))
    return out


async def extract(brand_code: str) -> None:
    """Read the stored manual and save what it says (runs in the background)."""
    row = await fetch_one(
        "SELECT p.*, b.name AS brand_name FROM brand_voice_profiles p JOIN brands b ON b.code = p.brand_code "
        "WHERE p.brand_code = $1", brand_code,
    )
    if not row or not row["manual_path"]:
        return
    path = STORAGE_ROOT / row["manual_path"]
    try:
        data = await asyncio.to_thread(path.read_bytes)
        facts = await asyncio.to_thread(read_pdf, data)
    except Exception as e:
        logger.warning("Couldn't open the brand manual for %s", brand_code, exc_info=True)
        await _save_failure(brand_code, f"Couldn't open the PDF: {e}")
        return

    status, error, ai = "DONE", None, None
    logo_png: Optional[bytes] = None
    try:
        ai = await _ask_ai(data, facts)
    except Exception as e:
        logger.warning("AI couldn't read the brand manual for %s: %s", brand_code, e)
        status = "PARTIAL"
        error = ("The AI couldn't read the manual right now, so only the colour codes, fonts and a logo guess were taken "
                 "straight from the PDF. Fill in the voice yourself, or read it again later.")

    if ai is not None:
        colors = [BrandColor(name=c.name.strip() or None, hex=h, usage=c.usage.strip() or None)
                  for c in ai.colors if (h := _hex(c.hex))]
        typefaces = [BrandTypeface(name=t.name.strip(), role=t.role.strip() or None, usage=t.usage.strip() or None)
                     for t in ai.typefaces if t.name.strip()]
        logo_index = ai.logo_image if 0 <= ai.logo_image < len(facts.images) else _logo_guess(facts)
        # A logo drawn as shapes: cut it out of the page where the AI found it.
        if 0 <= ai.logo_section < len(facts.sections):
            cropped = await asyncio.to_thread(crop_logo, data, facts.sections[ai.logo_section], ai.logo_box)
            if cropped:
                logo_png = cropped
        voice = BrandVoiceIn(voice=ai.voice[:2000] or None, tone=ai.tone[:20], do_examples=ai.do_examples[:20],
                             dont_examples=ai.dont_examples[:20], colors=colors[:24] or [BrandColor(hex=h) for h in facts.hexes],
                             typefaces=typefaces[:12] or [BrandTypeface(name=f) for f in facts.fonts],
                             logo_notes=ai.logo_notes[:500] or None)
    else:
        logo_index = _logo_guess(facts)
        keep = await fetch_one("SELECT voice, tone, do_examples, dont_examples, logo_notes FROM brand_voice_profiles "
                               "WHERE brand_code = $1", brand_code)
        voice = BrandVoiceIn(voice=keep["voice"], tone=list(keep["tone"] or []), do_examples=list(keep["do_examples"] or []),
                             dont_examples=list(keep["dont_examples"] or []), logo_notes=keep["logo_notes"],
                             colors=[BrandColor(hex=h) for h in facts.hexes],
                             typefaces=[BrandTypeface(name=f) for f in facts.fonts])
    voice = _clean(voice)
    if ai is None and not (voice.colors or voice.typefaces or logo_index is not None or logo_png):
        status = "FAILED"
        error = "Nothing could be read from this PDF. Fill in the brand voice yourself, or upload another file."

    palette = build_palette(facts.swatch_rows, [c.hex for c in voice.colors])
    # Main colours the manual only shows as the start of a swatch row (no hex printed) still count as colours.
    known = {c.hex for c in voice.colors}
    for strip in palette:
        if strip.base_hex not in known:
            voice.colors.append(BrandColor(hex=strip.base_hex))
            known.add(strip.base_hex)

    logo_path, logo_mime = None, None
    if logo_png:
        target = STORAGE_ROOT / brand_code / "logo.png"
        await asyncio.to_thread(target.write_bytes, logo_png)
        logo_path, logo_mime = str(target.relative_to(STORAGE_ROOT)), "image/png"
    elif logo_index is not None:
        _, _, _, image, ext = facts.images[logo_index]
        ext = "jpg" if ext == "jpeg" else ext
        target = STORAGE_ROOT / brand_code / f"logo.{ext}"
        await asyncio.to_thread(target.write_bytes, image)
        logo_path = str(target.relative_to(STORAGE_ROOT))
        logo_mime = "image/jpeg" if ext == "jpg" else f"image/{ext}"

    await fetch_one(
        """
        UPDATE brand_voice_profiles SET
            voice = $2, tone = $3, do_examples = $4, dont_examples = $5, colors = $6::jsonb, typefaces = $7::jsonb,
            logo_notes = $8, logo_path = $9, logo_mime = $10, status = $11, error = $12, extracted_at = NOW(),
            processing_started_at = NULL, updated_at = NOW(), palette = $13::jsonb
        WHERE brand_code = $1 RETURNING brand_code
        """,
        brand_code, voice.voice, voice.tone, voice.do_examples, voice.dont_examples,
        json.dumps([c.model_dump() for c in voice.colors]), json.dumps([t.model_dump() for t in voice.typefaces]),
        voice.logo_notes, logo_path, logo_mime, status, error, json.dumps([p.model_dump() for p in palette]),
    )
    await _sync_context_entry(brand_code, row["brand_name"], row["manual_uploaded_by"])
    logger.info("Brand manual for %s read: %s", brand_code, status)


async def _save_failure(brand_code: str, error: str) -> None:
    await fetch_one(
        "UPDATE brand_voice_profiles SET status = 'FAILED', error = $2, processing_started_at = NULL WHERE brand_code = $1 "
        "RETURNING brand_code", brand_code, error,
    )


def schedule(brand_code: str) -> None:
    async def run():
        try:
            await extract(brand_code)
        except Exception as e:
            logger.exception("Reading the brand manual for %s failed", brand_code)
            await _save_failure(brand_code, f"Reading the manual failed: {e}")

    task = asyncio.create_task(run())
    _tasks.add(task)
    task.add_done_callback(_tasks.discard)


# --------------------------------------------------------------------------------------------------------------------
# The Messaging Context entry the analysis reads
# --------------------------------------------------------------------------------------------------------------------

def context_body(voice: BrandVoiceIn, palette: Optional[List[PaletteRow]] = None) -> str:
    """The brand voice as one Messaging Context entry body (what the analysis is given)."""
    lines = [voice.voice or ""]
    if voice.tone:
        lines.append("Tone: " + ", ".join(voice.tone) + ".")
    if voice.colors:
        lines.append("Brand colours: " + "; ".join(
            " ".join(p for p in [c.name, c.hex, f"({c.usage})" if c.usage else None] if p) for c in voice.colors) + ".")
    if palette:
        # The manual's tints and shades are on-brand too: say how many there are of each main colour.
        groups: Dict[str, List[str]] = {}
        for r in palette:
            groups.setdefault(r.base_hex, []).append(f"{len(r.colors)} {r.label.lower()}")
        names = {c.hex: c.name for c in voice.colors if c.name}
        lines.append("Extended palette (also on-brand): " + "; ".join(
            f"{names.get(base, base)}: {', '.join(parts)}" for base, parts in groups.items()) + ".")
    if voice.typefaces:
        lines.append("Typefaces: " + "; ".join(
            " ".join(p for p in [t.name, f"({t.role})" if t.role else None] if p) for t in voice.typefaces) + ".")
    if voice.logo_notes:
        lines.append("Logo: " + voice.logo_notes)
    return "\n".join(line for line in lines if line.strip())[:2000]


async def _sync_context_entry(brand_code: str, brand_name: str, user_id: Optional[UUID]) -> None:
    """Keep the brand's Brand Voice entry in the Messaging Context in step with its profile."""
    from services.compliance.evaluation import reference_index_service

    row = await fetch_one("SELECT * FROM brand_voice_profiles WHERE brand_code = $1", brand_code)
    voice = BrandVoiceIn(voice=row["voice"], tone=list(row["tone"] or []), do_examples=list(row["do_examples"] or []),
                         dont_examples=list(row["dont_examples"] or []), logo_notes=row["logo_notes"],
                         colors=[BrandColor(**c) for c in _json_list(row["colors"])],
                         typefaces=[BrandTypeface(**t) for t in _json_list(row["typefaces"])])
    body = context_body(voice, [PaletteRow(**p) for p in _json_list(row["palette"])])
    do = "\n".join(voice.do_examples)[:1000] or None
    dont = "\n".join(voice.dont_examples)[:1000] or None
    source = f"Brand manual: {row['manual_name']}" if row["manual_name"] else "Brand voice"
    existing = await fetch_one(
        "SELECT id FROM messaging_context WHERE auto_source = $1 AND brands = ARRAY[$2]::text[] AND deleted_at IS NULL "
        "AND kind = 'BRAND_VOICE'", AUTO_SOURCE, brand_code,
    )
    if not body:
        if existing:
            await fetch_one("UPDATE messaging_context SET deleted_at = NOW() WHERE id = $1 RETURNING id", existing["id"])
            reference_index_service.sync_soon()
        return
    if existing:
        await fetch_one(
            """
            UPDATE messaging_context SET title = $2, body = $3, do_examples = $4, dont_examples = $5, source = $6,
                   auto_body = $3, is_active = true, updated_at = NOW(), updated_by = $7
            WHERE id = $1 RETURNING id
            """,
            existing["id"], f"{brand_name} brand voice", body, do, dont, source[:300], user_id,
        )
    else:
        await fetch_one(
            """
            INSERT INTO messaging_context (kind, title, body, do_examples, dont_examples, brands, source, auto_source,
                                           auto_body, created_by, updated_by)
            VALUES ('BRAND_VOICE', $1, $2, $3, $4, ARRAY[$5]::text[], $6, $7, $2, $8, $8) RETURNING id
            """,
            f"{brand_name} brand voice", body, do, dont, brand_code, source[:300], AUTO_SOURCE, user_id,
        )
    reference_index_service.sync_soon()


async def resume_interrupted() -> int:
    """Manuals that were being read when the server stopped are read again."""
    rows = await fetch_all("SELECT brand_code FROM brand_voice_profiles WHERE status = 'PROCESSING'")
    for r in rows:
        schedule(r["brand_code"])
    return len(rows)
