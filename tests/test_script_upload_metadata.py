"""Exercise the actual upload entry points with isolated I/O dependencies."""
import ast
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from services.compliance.evaluation.script_import_sections import script_import_sections
from services.compliance.evaluation.script_import_format import script_import_warnings
from services.compliance.evaluation.script_split import split_scripts

TEXT = "# Collection title\n# 1 First script\n| Timecode | Visuals | Voiceover | Supers |\n| 0:00 | Camera | First | FIRST |\n# 2 Second script\n| Timecode | Visuals | Voiceover | Supers |\n| 0:00 | Camera | Second | SECOND |"


def upload_namespace():
    path = Path(__file__).parents[1] / "services/compliance/evaluation/script_doc_pages.py"
    tree = ast.parse(path.read_text(encoding="utf-8"))
    entry_points = [node for node in tree.body if isinstance(node, ast.AsyncFunctionDef)
                    and node.name in {"create_from_upload", "upload_pages"}]
    namespace = {
        "UUID": object, "UploadedPages": SimpleNamespace,
        "batches": SimpleNamespace(read_upload=AsyncMock(return_value=TEXT)),
        "fetch_one": AsyncMock(return_value={"id": "set-id"}),
        "execute": AsyncMock(), "_owned_set": AsyncMock(return_value=({}, "set-id")),
        "_add_uploaded": AsyncMock(return_value=["first-id", "second-id"]),
        "script_import_sections": script_import_sections,
        "script_import_warnings": script_import_warnings, "split_scripts": split_scripts,
    }
    exec(compile(ast.Module(body=entry_points, type_ignores=[]), str(path), "exec"), namespace)
    return namespace


@pytest.mark.asyncio
async def test_new_collection_uses_document_title_and_returns_format_notice():
    namespace = upload_namespace()
    result = await namespace["create_from_upload"]("creator", "filename.pdf", b"pdf")
    assert namespace["fetch_one"].await_args.args[1] == "Collection title"
    assert result.created == ["first-id", "second-id"]
    assert "export template format" in result.warnings[0]
    namespace["batches"].read_upload.assert_awaited_once()
    assert namespace["_add_uploaded"].await_args.args[-1] == TEXT


@pytest.mark.asyncio
async def test_titleless_upload_retains_filename_for_collection_name():
    namespace = upload_namespace()
    namespace["batches"].read_upload.return_value = TEXT.replace("# Collection title\n", "")
    await namespace["create_from_upload"]("creator", "filename.pdf", b"pdf")
    assert namespace["fetch_one"].await_args.args[1] == "filename"


@pytest.mark.asyncio
async def test_upload_into_existing_collection_keeps_set_and_returns_notice():
    namespace = upload_namespace()
    result = await namespace["upload_pages"]("doc", "creator", "filename.pdf", b"pdf")
    assert result.warnings
    namespace["fetch_one"].assert_not_awaited()
    assert namespace["_add_uploaded"].await_args.args[0] == "set-id"
