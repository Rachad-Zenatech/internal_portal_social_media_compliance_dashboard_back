"""Resolve brand manual downloads and restore missing local files from object storage."""

import asyncio
from pathlib import Path

from fastapi import HTTPException

from postgresql_db.database import fetch_one
from services.compliance.media.file_mime_policy import safe_file_mime
from services.shared.storage.s3_storage_service import ensure_local_file


async def stored_file(root: Path, brand_code: str, which: str):
    row = await fetch_one("SELECT * FROM brand_voice_profiles WHERE brand_code = $1", brand_code)
    relative = row and (row["manual_path"] if which == "manual" else row["logo_path"])
    if not relative:
        raise HTTPException(status_code=404, detail="Not found")
    path = (root / relative).resolve()
    if root not in path.parents:
        raise HTTPException(status_code=404, detail="Not found")
    if not path.is_file():
        await asyncio.to_thread(ensure_local_file, path, "brand_voice", path.relative_to(root).as_posix())
    if not path.is_file():
        raise HTTPException(status_code=404, detail="This brand file is unavailable. Please upload the manual again.")
    if which == "manual":
        return path, "application/pdf", row["manual_name"] or f"{brand_code}-brand-manual.pdf"
    return path, safe_file_mime(path.name), f"{brand_code}-logo{path.suffix}"
