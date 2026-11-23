"""Recover small-text locations from enlarged sections; map boxes back to the original image."""
import asyncio
import io
import json
import logging
import math

from google.genai import types

from services.compliance.media import image_text_service

logger = logging.getLogger(__name__)


def sections(data):
    from PIL import Image

    with Image.open(io.BytesIO(data)) as image:
        image = image.convert("RGB")
        width, height = image.size
        if width < 2 or height < 2:
            return []
        tiles = []
        for left, right in ((0, math.ceil(width * .6)), (math.floor(width * .4), width)):
            for top, bottom in ((0, math.ceil(height * .6)), (math.floor(height * .4), height)):
                tile = image.crop((left, top, right, bottom))
                scale = min(4, 1600 / max(tile.size))
                tile = tile.resize((max(1, round(tile.width * scale)), max(1, round(tile.height * scale))),
                                   Image.Resampling.LANCZOS)
                encoded = io.BytesIO()
                tile.save(encoded, format="PNG")
                tiles.append((encoded.getvalue(), (left, top, right, bottom), (width, height)))
        return tiles


def original_box(box, bounds, size):
    if not isinstance(box, list) or len(box) != 4:
        return None
    if any(isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v) for v in box):
        return None
    top, left, bottom, right = box
    if not (0 <= top < bottom <= 1000 and 0 <= left < right <= 1000):
        return None
    # Outlining a whole crop is not evidence of a word's position.
    if (bottom - top) * (right - left) > 750_000:
        return None
    x1, y1, x2, y2 = bounds
    width, height = size
    return [(y1 * 1000 + top * (y2 - y1)) / height,
            (x1 * 1000 + left * (x2 - x1)) / width,
            (y1 * 1000 + bottom * (y2 - y1)) / height,
            (x1 * 1000 + right * (x2 - x1)) / width]


async def locate(data, prompt, schema):
    try:
        tiles = await asyncio.to_thread(sections, data)
    except Exception:
        logger.debug("Image localization could not prepare enlarged sections", exc_info=True)
        return []

    async def read(tile):
        encoded, bounds, size = tile
        try:
            async with image_text_service._semaphore:
                response = await image_text_service._generate(
                    [types.Part.from_bytes(data=encoded, mime_type="image/png"), prompt +
                     " This is an enlarged crop. Give coordinates relative to this attached crop only."],
                    feature="IMAGE_FINDING_REGIONS", response_schema=schema)
            items = json.loads(response.text or "{}").get("regions", [])
            mapped = []
            for item in items if isinstance(items, list) else []:
                box = original_box(item.get("box"), bounds, size) if isinstance(item, dict) else None
                if box:
                    mapped.append({**item, "box": box})
            return mapped
        except Exception:
            logger.warning("Image localization could not read an enlarged section", exc_info=True)
            return []

    return [item for result in await asyncio.gather(*(read(tile) for tile in tiles)) for item in result]
