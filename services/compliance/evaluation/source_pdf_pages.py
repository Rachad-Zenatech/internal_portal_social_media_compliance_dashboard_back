"""Split source PDFs with Poppler in bounded subprocesses, independent of text extraction."""
import os
import re
import subprocess
import tempfile
from pathlib import Path


def _run(args, **kwargs):
    result = subprocess.run(args, capture_output=True, timeout=60, check=False, **kwargs)
    if result.returncode:
        raise ValueError("The PDF could not be split into readable pages")
    return result.stdout


def batches(data):
    info = _run(["pdfinfo", "-"], input=data).decode("utf-8", "replace")
    match = re.search(r"^Pages:\s+(\d+)", info, re.MULTILINE)
    count = int(match.group(1)) if match else 0
    if not 0 < count <= int(os.getenv("SOURCE_PDF_AI_MAX_PAGES", "100")):
        raise ValueError("PDF page count is missing or exceeds the reading limit")
    size = max(1, min(4, int(os.getenv("SOURCE_PDF_AI_BATCH_PAGES", "2"))))
    with tempfile.TemporaryDirectory(prefix="source-pdf-") as folder:
        root = Path(folder)
        source = root / "source.pdf"
        source.write_bytes(data)
        _run(["pdfseparate", "-f", "1", "-l", str(count), str(source), str(root / "page-%d.pdf")])
        result = []
        for start in range(1, count + 1, size):
            end = min(start + size - 1, count)
            pages = [root / f"page-{n}.pdf" for n in range(start, end + 1)]
            target = root / f"batch-{start}.pdf"
            if len(pages) == 1:
                payload = pages[0].read_bytes()
            else:
                _run(["pdfunite", *map(str, pages), str(target)])
                payload = target.read_bytes()
            if len(payload) > 4 * 1024 * 1024:
                raise ValueError("A PDF page batch exceeds the AI attachment size limit")
            result.append((start, end, payload))
        return result
