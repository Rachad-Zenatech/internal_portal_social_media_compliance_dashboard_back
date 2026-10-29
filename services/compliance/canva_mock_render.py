"""Renders the mock Canva designs to real PNG and MP4 files.

Frames are drawn in plain Python (a gradient background, an accent bar and blocky text from a built-in 5x7 pixel
font, so no font files or imaging libraries are needed) and encoded by the ffmpeg binary that imageio-ffmpeg
bundles. The text is real on-screen text, so transcription, the compliance check and sentiment analysis all see
it, and a design's revisions can fix wording the check flagged.
"""

import asyncio
import subprocess
import tempfile
from pathlib import Path
from typing import List, Sequence, Tuple

RGB = Tuple[int, int, int]

# 5x7 glyphs; "#" is a lit pixel. Text is drawn in capitals.
_GLYPHS = {
    "A": [".###.", "#...#", "#...#", "#####", "#...#", "#...#", "#...#"],
    "B": ["####.", "#...#", "#...#", "####.", "#...#", "#...#", "####."],
    "C": [".###.", "#...#", "#....", "#....", "#....", "#...#", ".###."],
    "D": ["####.", "#...#", "#...#", "#...#", "#...#", "#...#", "####."],
    "E": ["#####", "#....", "#....", "####.", "#....", "#....", "#####"],
    "F": ["#####", "#....", "#....", "####.", "#....", "#....", "#...."],
    "G": [".###.", "#...#", "#....", "#.###", "#...#", "#...#", ".####"],
    "H": ["#...#", "#...#", "#...#", "#####", "#...#", "#...#", "#...#"],
    "I": [".###.", "..#..", "..#..", "..#..", "..#..", "..#..", ".###."],
    "J": ["..###", "...#.", "...#.", "...#.", "...#.", "#..#.", ".##.."],
    "K": ["#...#", "#..#.", "#.#..", "##...", "#.#..", "#..#.", "#...#"],
    "L": ["#....", "#....", "#....", "#....", "#....", "#....", "#####"],
    "M": ["#...#", "##.##", "#.#.#", "#.#.#", "#...#", "#...#", "#...#"],
    "N": ["#...#", "#...#", "##..#", "#.#.#", "#..##", "#...#", "#...#"],
    "O": [".###.", "#...#", "#...#", "#...#", "#...#", "#...#", ".###."],
    "P": ["####.", "#...#", "#...#", "####.", "#....", "#....", "#...."],
    "Q": [".###.", "#...#", "#...#", "#...#", "#.#.#", "#..#.", ".##.#"],
    "R": ["####.", "#...#", "#...#", "####.", "#.#..", "#..#.", "#...#"],
    "S": [".####", "#....", "#....", ".###.", "....#", "....#", "####."],
    "T": ["#####", "..#..", "..#..", "..#..", "..#..", "..#..", "..#.."],
    "U": ["#...#", "#...#", "#...#", "#...#", "#...#", "#...#", ".###."],
    "V": ["#...#", "#...#", "#...#", "#...#", "#...#", ".#.#.", "..#.."],
    "W": ["#...#", "#...#", "#...#", "#.#.#", "#.#.#", "#.#.#", ".#.#."],
    "X": ["#...#", "#...#", ".#.#.", "..#..", ".#.#.", "#...#", "#...#"],
    "Y": ["#...#", "#...#", ".#.#.", "..#..", "..#..", "..#..", "..#.."],
    "Z": ["#####", "....#", "...#.", "..#..", ".#...", "#....", "#####"],
    "0": [".###.", "#...#", "#..##", "#.#.#", "##..#", "#...#", ".###."],
    "1": ["..#..", ".##..", "..#..", "..#..", "..#..", "..#..", ".###."],
    "2": [".###.", "#...#", "....#", "...#.", "..#..", ".#...", "#####"],
    "3": ["####.", "....#", "....#", ".###.", "....#", "....#", "####."],
    "4": ["...#.", "..##.", ".#.#.", "#..#.", "#####", "...#.", "...#."],
    "5": ["#####", "#....", "####.", "....#", "....#", "#...#", ".###."],
    "6": ["..##.", ".#...", "#....", "####.", "#...#", "#...#", ".###."],
    "7": ["#####", "....#", "...#.", "..#..", ".#...", ".#...", ".#..."],
    "8": [".###.", "#...#", "#...#", ".###.", "#...#", "#...#", ".###."],
    "9": [".###.", "#...#", "#...#", ".####", "....#", "...#.", ".##.."],
    " ": [".....", ".....", ".....", ".....", ".....", ".....", "....."],
    ".": [".....", ".....", ".....", ".....", ".....", ".##..", ".##.."],
    ",": [".....", ".....", ".....", ".....", ".##..", "..#..", ".#..."],
    "!": ["..#..", "..#..", "..#..", "..#..", "..#..", ".....", "..#.."],
    "?": [".###.", "#...#", "....#", "...#.", "..#..", ".....", "..#.."],
    ":": [".....", ".##..", ".##..", ".....", ".##..", ".##..", "....."],
    "'": ["..#..", "..#..", ".#...", ".....", ".....", ".....", "....."],
    "-": [".....", ".....", ".....", "#####", ".....", ".....", "....."],
    "&": [".##..", "#..#.", "#.#..", ".#...", "#.#.#", "#..#.", ".##.#"],
    "/": ["....#", "...#.", "...#.", "..#..", ".#...", ".#...", "#...."],
    "%": ["##..#", "##..#", "...#.", "..#..", ".#...", "#..##", "#..##"],
    "$": ["..#..", ".####", "#.#..", ".###.", "..#.#", "####.", "..#.."],
    "+": [".....", "..#..", "..#..", "#####", "..#..", "..#..", "....."],
    "#": [".#.#.", ".#.#.", "#####", ".#.#.", "#####", ".#.#.", ".#.#."],
    "@": [".###.", "#...#", "#.###", "#.#.#", "#.###", "#....", ".###."],
}
_GLYPH_W, _GLYPH_H = 5, 7

# Text size names to pixel scale, relative to the frame width.
_SIZES = {"xl": 90, "l": 120, "m": 165, "s": 240}

# One block of text: (words, size name, colour).
TextLine = Tuple[str, str, RGB]


class Frame:
    """An RGB frame being drawn."""

    def __init__(self, width: int, height: int, top: RGB, bottom: RGB):
        self.width, self.height = width, height
        rows = []
        for y in range(height):
            t = y / max(1, height - 1)
            colour = bytes(round(a + (b - a) * t) for a, b in zip(top, bottom))
            rows.append(colour * width)
        self.pixels = bytearray(b"".join(rows))

    def rect(self, x: int, y: int, w: int, h: int, colour: RGB) -> None:
        x0, y0 = max(0, x), max(0, y)
        x1, y1 = min(self.width, x + w), min(self.height, y + h)
        if x1 <= x0 or y1 <= y0:
            return
        span = bytes(colour) * (x1 - x0)
        for yy in range(y0, y1):
            start = (yy * self.width + x0) * 3
            self.pixels[start:start + len(span)] = span

    def text(self, words: str, x: int, y: int, scale: int, colour: RGB) -> None:
        for i, ch in enumerate(words):
            glyph = _GLYPHS.get(ch) or _GLYPHS["?"]
            gx = x + i * (_GLYPH_W + 1) * scale
            for row, bits in enumerate(glyph):
                for col, bit in enumerate(bits):
                    if bit == "#":
                        self.rect(gx + col * scale, y + row * scale, scale, scale, colour)


def _wrap(words: str, max_chars: int) -> List[str]:
    lines, current = [], ""
    for word in words.split():
        while len(word) > max_chars:  # a word longer than the line: break it
            if current:
                lines.append(current)
                current = ""
            lines.append(word[:max_chars])
            word = word[max_chars:]
        candidate = f"{current} {word}".strip()
        if len(candidate) <= max_chars:
            current = candidate
        else:
            lines.append(current)
            current = word
    if current:
        lines.append(current)
    return lines


def draw_scene(width: int, height: int, top: RGB, bottom: RGB, accent: RGB, lines: Sequence[TextLine], footer: str) -> bytes:
    """One frame: the text block centred vertically, an accent bar above it and a small footer."""
    frame = Frame(width, height, top, bottom)
    margin = width // 12
    laid: List[Tuple[str, int, RGB]] = []
    for words, size, colour in lines:
        scale = max(2, width // _SIZES.get(size, _SIZES["m"]))
        max_chars = max(4, (width - 2 * margin) // ((_GLYPH_W + 1) * scale))
        laid += [(line, scale, colour) for line in _wrap(words.upper(), max_chars)]
    def gap(scale: int) -> int:  # line height
        return (_GLYPH_H + 3) * scale

    block_h = sum(gap(scale) for _, scale, _ in laid)
    y = max(margin, (height - block_h) // 2)
    frame.rect(margin, y - width // 20, width // 6, max(4, width // 120), accent)
    for line, scale, colour in laid:
        line_w = len(line) * (_GLYPH_W + 1) * scale - scale
        frame.text(line, (width - line_w) // 2, y, scale, colour)
        y += gap(scale)
    small = max(2, width // 320)
    footer = footer.upper()
    frame.text(footer, (width - (len(footer) * (_GLYPH_W + 1) * small)) // 2, height - margin // 2 - _GLYPH_H * small, small, (220, 225, 235))
    return bytes(frame.pixels)


def _ffmpeg() -> str:
    try:
        import imageio_ffmpeg
    except ImportError as e:  # pragma: no cover - dependency missing
        raise RuntimeError("Mock Canva exports need imageio-ffmpeg (pip install -r requirements.txt).") from e
    return imageio_ffmpeg.get_ffmpeg_exe()


def _run(args: List[str], data: bytes, timeout: int = 120) -> bytes:
    done = subprocess.run([_ffmpeg(), "-hide_banner", "-loglevel", "error", *args], input=data,
                          capture_output=True, timeout=timeout, check=False)
    if done.returncode != 0:
        raise RuntimeError(f"ffmpeg failed: {done.stderr.decode(errors='replace')[:300]}")
    return done.stdout


async def encode_png(frame: bytes, width: int, height: int) -> bytes:
    return await asyncio.to_thread(
        _run,
        ["-f", "rawvideo", "-pix_fmt", "rgb24", "-s", f"{width}x{height}", "-i", "pipe:0",
         "-frames:v", "1", "-c:v", "png", "-f", "image2pipe", "pipe:1"],
        frame,
    )


async def encode_mp4(frames: Sequence[bytes], width: int, height: int, seconds_per_frame: float) -> bytes:
    """H.264 MP4 showing each frame for seconds_per_frame (silent), playable in browsers."""
    with tempfile.TemporaryDirectory() as tmp:
        out = Path(tmp) / "design.mp4"
        await asyncio.to_thread(
            _run,
            ["-f", "rawvideo", "-pix_fmt", "rgb24", "-s", f"{width}x{height}",
             "-framerate", f"{1 / seconds_per_frame:.4f}", "-i", "pipe:0",
             "-c:v", "libx264", "-preset", "veryfast", "-pix_fmt", "yuv420p", "-r", "24",
             "-movflags", "+faststart", "-y", str(out)],
            b"".join(frames),
        )
        return out.read_bytes()
