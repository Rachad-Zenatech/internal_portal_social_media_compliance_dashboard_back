"""Canva integration: connect an account, list designs, and export a design so it can be added to a submission.

Until the Canva Connect API keys are configured (CANVA_CLIENT_ID), everything runs against a mock Canva with a few
demo designs, rendered to real PNG and MP4 files. Each user's mock designs have their own revision number, and
"simulate an edit" moves it on, so reimporting after changes can be tried end to end. The live provider is a stub
that marks where the Connect API calls go; the submission side (import, reimport, review) doesn't change.
"""

import os
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Dict, List, Sequence, Tuple
from uuid import UUID

from fastapi import HTTPException, status

from models.canva_model import CanvaConnection, CanvaDesign
from postgresql_db.database import fetch_all, fetch_one
from services.compliance import canva_mock_render as render
from services.compliance.canva_mock_render import RGB, TextLine


def mode() -> str:
    return "LIVE" if os.getenv("CANVA_CLIENT_ID") else "MOCK"


@dataclass
class DesignExport:
    """A design exported from Canva, ready to store as a submission file."""
    design: CanvaDesign
    data: bytes
    mime_type: str
    extension: str

    @property
    def file_name(self) -> str:
        safe = "".join(c if c.isalnum() or c in " -_" else "" for c in self.design.title).strip() or "canva-design"
        return f"{safe[:80]}{self.extension}"


# ---------------------------------------------------------------------------
# Mock Canva
# ---------------------------------------------------------------------------

WHITE: RGB = (255, 255, 255)


@dataclass(frozen=True)
class MockDesign:
    id: str
    title: str
    kind: str  # IMAGE or VIDEO
    width: int
    height: int
    top: RGB
    bottom: RGB
    accent: RGB
    # Each revision's scenes (one scene for an image, several for a video). Revisions past the last reuse it.
    revisions: Tuple[Tuple[Tuple[TextLine, ...], ...], ...] = field(default_factory=tuple)

    def scenes(self, revision: int) -> Sequence[Sequence[TextLine]]:
        return self.revisions[min(revision, len(self.revisions)) - 1]


_CYAN: RGB = (56, 189, 248)
_AMBER: RGB = (251, 191, 36)
_LIME: RGB = (163, 230, 53)

# Revision 1 of several designs uses wording the rulebook flags, and revision 2 fixes it, so the reviewer's
# "request changes" -> edit in Canva -> reimport loop shows the check passing the second time.
MOCK_DESIGNS: Dict[str, MockDesign] = {d.id: d for d in [
    MockDesign(
        id="mock-zenadrone-reel", title="ZenaDrone 1000 field trials - Instagram Reel", kind="VIDEO",
        width=720, height=1280, top=(15, 23, 42), bottom=(2, 6, 23), accent=_CYAN,
        revisions=(
            (
                (("ZenaDrone 1000", "xl", WHITE), ("Field trials", "l", _CYAN)),
                (("Combat proven and now shipping", "l", WHITE),),
                (("Order yours today", "l", WHITE), ("zenatech.com", "m", _CYAN)),
            ),
            (
                (("ZenaDrone 1000", "xl", WHITE), ("Field trials", "l", _CYAN)),
                (("In field testing with commercial partners", "l", WHITE),),
                (("Learn more", "l", WHITE), ("zenatech.com", "m", _CYAN)),
            ),
        ),
    ),
    MockDesign(
        id="mock-zoooffice-promo", title="ZooOffice promo video", kind="VIDEO",
        width=1280, height=720, top=(46, 16, 101), bottom=(15, 5, 40), accent=_AMBER,
        revisions=(
            (
                (("ZooOffice", "xl", WHITE), ("Smarter workspaces", "l", _AMBER)),
                (("Fully autonomous building management", "l", WHITE),),
                (("Book a demo", "l", WHITE), ("zooffice.com", "m", _AMBER)),
            ),
            (
                (("ZooOffice", "xl", WHITE), ("Smarter workspaces", "l", _AMBER)),
                (("AI-assisted building management", "l", WHITE),),
                (("Book a demo", "l", WHITE), ("zooffice.com", "m", _AMBER)),
            ),
        ),
    ),
    MockDesign(
        id="mock-q3-investor-graphic", title="Q3 investor update - LinkedIn graphic", kind="IMAGE",
        width=1080, height=1080, top=(6, 78, 59), bottom=(2, 44, 34), accent=_LIME,
        revisions=(
            ((("Q3 update", "l", _LIME), ("Record drone revenue", "xl", WHITE),
              ("Guaranteed returns for shareholders", "m", WHITE)),),
            ((("Q3 update", "l", _LIME), ("Record drone revenue", "xl", WHITE),
              ("Read the full release at zenatech.com", "m", WHITE)),),
        ),
    ),
    MockDesign(
        id="mock-daas-instagram", title="Drone as a Service launch - Instagram post", kind="IMAGE",
        width=1080, height=1350, top=(124, 45, 18), bottom=(67, 20, 7), accent=_AMBER,
        revisions=(
            ((("DaaS", "xl", WHITE), ("Drone as a Service", "l", _AMBER), ("Now in 12 cities", "m", WHITE)),),
            ((("DaaS", "xl", WHITE), ("Drone as a Service", "l", _AMBER), ("Now in 15 cities", "m", WHITE)),),
        ),
    ),
    MockDesign(
        id="mock-spring-ad", title="ZenaDrone spring campaign - ad creative", kind="IMAGE",
        width=1080, height=1080, top=(30, 58, 138), bottom=(12, 24, 64), accent=_CYAN,
        revisions=(
            ((("Spring sale", "l", _CYAN), ("ZenaDrone 1000", "xl", WHITE), ("Now shipping - 20% off", "m", WHITE)),),
            ((("Spring offer", "l", _CYAN), ("ZenaDrone 1000", "xl", WHITE), ("Pre-order with 20% off", "m", WHITE)),),
        ),
    ),
]}

VIDEO_SECONDS_PER_SCENE = 2.5
_thumbnail_cache: Dict[Tuple[str, int], bytes] = {}
_THUMBNAIL_CACHE_MAX = 64


def _footer(revision: int) -> str:
    return f"Mock Canva design - revision {revision}"


def _mock_design(design_id: str) -> MockDesign:
    design = MOCK_DESIGNS.get(design_id)
    if not design:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="That Canva design wasn't found")
    return design


async def _mock_revisions(user_id: UUID) -> Dict[str, Tuple[int, datetime]]:
    rows = await fetch_all(
        "SELECT design_id, revision, updated_at FROM canva_mock_revisions WHERE user_id = $1", user_id
    )
    return {r["design_id"]: (r["revision"], r["updated_at"]) for r in rows}


def _to_design(d: MockDesign, revision: int, updated_at: datetime) -> CanvaDesign:
    return CanvaDesign(
        id=d.id, title=d.title, kind=d.kind, width=d.width, height=d.height, revision=revision,
        updated_at=updated_at, thumbnail_url=f"/api/canva/designs/{d.id}/thumbnail?rev={revision}",
    )


# ---------------------------------------------------------------------------
# Connection
# ---------------------------------------------------------------------------

def _require_mock() -> None:
    if mode() != "MOCK":
        # Live mode: OAuth (PKCE) to Canva, then the Connect API's designs and exports endpoints.
        raise HTTPException(status_code=status.HTTP_501_NOT_IMPLEMENTED,
                            detail="The live Canva connection isn't set up yet")


async def get_connection(user_id: UUID) -> CanvaConnection:
    row = await fetch_one("SELECT display_name, connected_at FROM canva_connections WHERE user_id = $1", user_id)
    return CanvaConnection(
        connected=bool(row), mode=mode(),
        display_name=row["display_name"] if row else None, connected_at=row["connected_at"] if row else None,
    )


async def connect(user_id: UUID) -> CanvaConnection:
    """Mock: connects straight away. Live, this becomes the start of Canva's OAuth flow."""
    _require_mock()
    user = await fetch_one("SELECT full_name, email FROM users WHERE id = $1", user_id)
    name = f"{(user['full_name'] or user['email']) if user else 'Demo'} (demo Canva)"
    await fetch_one(
        """
        INSERT INTO canva_connections (user_id, mode, display_name) VALUES ($1, 'MOCK', $2)
        ON CONFLICT (user_id) DO UPDATE SET display_name = EXCLUDED.display_name, connected_at = NOW()
        RETURNING user_id
        """,
        user_id, name,
    )
    return await get_connection(user_id)


async def disconnect(user_id: UUID) -> None:
    await fetch_one("DELETE FROM canva_connections WHERE user_id = $1 RETURNING user_id", user_id)


async def _require_connection(user_id: UUID) -> CanvaConnection:
    conn = await get_connection(user_id)
    if not conn.connected:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Connect your Canva account first")
    return conn


# ---------------------------------------------------------------------------
# Designs and exports
# ---------------------------------------------------------------------------

async def list_designs(user_id: UUID) -> List[CanvaDesign]:
    conn = await _require_connection(user_id)
    _require_mock()
    revisions = await _mock_revisions(user_id)
    since = conn.connected_at or datetime.now(timezone.utc)
    return [_to_design(d, *revisions.get(d.id, (1, since))) for d in MOCK_DESIGNS.values()]


async def get_design(user_id: UUID, design_id: str) -> CanvaDesign:
    conn = await _require_connection(user_id)
    _require_mock()
    d = _mock_design(design_id)
    revision, updated_at = (await _mock_revisions(user_id)).get(d.id, (1, conn.connected_at or datetime.now(timezone.utc)))
    return _to_design(d, revision, updated_at)


async def simulate_edit(user_id: UUID, design_id: str) -> CanvaDesign:
    """Mock only: stands in for the creator editing the design in Canva, so its next export is a new revision."""
    await _require_connection(user_id)
    _require_mock()
    _mock_design(design_id)
    await fetch_one(
        """
        INSERT INTO canva_mock_revisions (user_id, design_id, revision) VALUES ($1, $2, 2)
        ON CONFLICT (user_id, design_id) DO UPDATE SET revision = canva_mock_revisions.revision + 1, updated_at = NOW()
        RETURNING revision
        """,
        user_id, design_id,
    )
    return await get_design(user_id, design_id)


async def export_design(user_id: UUID, design_id: str) -> DesignExport:
    """The design's latest revision as a file: MP4 for videos, PNG for images."""
    design = await get_design(user_id, design_id)
    d = _mock_design(design_id)
    try:
        frames = [render.draw_scene(d.width, d.height, d.top, d.bottom, d.accent, scene, _footer(design.revision))
                  for scene in d.scenes(design.revision)]
        if d.kind == "VIDEO":
            data = await render.encode_mp4(frames, d.width, d.height, VIDEO_SECONDS_PER_SCENE)
            return DesignExport(design=design, data=data, mime_type="video/mp4", extension=".mp4")
        data = await render.encode_png(frames[0], d.width, d.height)
        return DesignExport(design=design, data=data, mime_type="image/png", extension=".png")
    except RuntimeError as e:
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=f"Canva export failed: {e}")


async def thumbnail(user_id: UUID, design_id: str) -> bytes:
    """A small PNG of the design's first scene, for the picker."""
    design = await get_design(user_id, design_id)
    d = _mock_design(design_id)
    key = (f"{user_id}:{design_id}", design.revision)
    if key not in _thumbnail_cache:
        w, h = (d.width // 3) // 2 * 2, (d.height // 3) // 2 * 2
        frame = render.draw_scene(w, h, d.top, d.bottom, d.accent, d.scenes(design.revision)[0], _footer(design.revision))
        try:
            png = await render.encode_png(frame, w, h)
        except RuntimeError as e:
            raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=f"Couldn't draw the preview: {e}")
        if len(_thumbnail_cache) >= _THUMBNAIL_CACHE_MAX:
            _thumbnail_cache.pop(next(iter(_thumbnail_cache)))
        _thumbnail_cache[key] = png
    return _thumbnail_cache[key]


def design_kind_ok(kind: str, allowed_extensions: set) -> bool:
    """Whether an export of this kind can be the main file of a submission that takes these extensions."""
    return (".mp4" if kind == "VIDEO" else ".png") in allowed_extensions
