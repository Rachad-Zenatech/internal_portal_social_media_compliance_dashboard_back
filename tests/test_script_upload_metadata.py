"""Exercise the actual upload entry points with isolated I/O dependencies."""
import asyncio
import ast
import json
import re
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Dict, List, Optional
from unittest.mock import AsyncMock

import pytest

from services.compliance.evaluation.script_import_sections import script_import_sections
from services.compliance.evaluation.script_import_format import script_import_warnings
from services.compliance.evaluation.script_split import split_scripts
from services.compliance.media.script_sources import extract

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


def test_new_collection_uses_document_title_and_returns_format_notice():
    namespace = upload_namespace()
    result = asyncio.run(namespace["create_from_upload"]("creator", "filename.pdf", b"pdf"))
    assert namespace["fetch_one"].await_args.args[1] == "Collection title"
    assert result.created == ["first-id", "second-id"]
    assert "export template format" in result.warnings[0]
    namespace["batches"].read_upload.assert_awaited_once()
    assert namespace["_add_uploaded"].await_args.args[-1] == TEXT


def test_titleless_upload_retains_filename_for_collection_name():
    namespace = upload_namespace()
    namespace["batches"].read_upload.return_value = TEXT.replace("# Collection title\n", "")
    asyncio.run(namespace["create_from_upload"]("creator", "filename.pdf", b"pdf"))
    assert namespace["fetch_one"].await_args.args[1] == "filename"


def test_upload_into_existing_collection_keeps_set_and_returns_notice():
    namespace = upload_namespace()
    result = asyncio.run(namespace["upload_pages"]("doc", "creator", "filename.pdf", b"pdf"))
    assert result.warnings
    namespace["fetch_one"].assert_not_awaited()
    assert namespace["_add_uploaded"].await_args.args[0] == "set-id"


def persistence_namespace(filename, names):
    path = Path(__file__).parents[1] / "services/compliance/evaluation" / filename
    tree = ast.parse(path.read_text(encoding="utf-8"))
    functions = [node for node in tree.body if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name in names]
    namespace = {
        "UUID": object, "Optional": Optional, "List": List, "Dict": Dict, "Any": Any,
        "DocPage": object, "DocPost": object, "DocDetail": SimpleNamespace, "DocAccess": object,
        "json": json, "re": re, "extract": extract, "split_scripts": split_scripts,
        "script_import_warnings": script_import_warnings,
        "subs": SimpleNamespace(_json=lambda value: json.loads(value) if isinstance(value, str) else value),
    }
    exec(compile(ast.Module(body=functions, type_ignores=[]), str(path), "exec"), namespace)
    return namespace


@pytest.mark.parametrize("warnings", [[], ["Template format differs"]])
def test_page_creation_persists_import_notice_in_immutable_event(warnings):
    ns = persistence_namespace("script_doc_pages.py", {"_new_page"})
    ns.update(fetch_one=AsyncMock(side_effect=[{"details": {}}, {"id": "page"}]), execute=AsyncMock(),
              core=SimpleNamespace(_event=AsyncMock()), shared=SimpleNamespace(ensure_shared_document=AsyncMock()))
    asyncio.run(ns["_new_page"]("set", "creator", "Title", "<p>Script</p>", import_warnings=warnings))
    assert ns["core"]._event.await_args.args == ("page", 0, "creator", "CREATED", {"page": True, "import_warnings": warnings})


def test_every_imported_page_retains_the_original_file_notice():
    ns = persistence_namespace("script_doc_pages.py", {"_add_uploaded"})
    ns.update(_pages=AsyncMock(side_effect=[[], [SimpleNamespace(id="first"), SimpleNamespace(id="second")]]),
              _new_page=AsyncMock(side_effect=["first", "second"]), log_audit_event=AsyncMock(),
              batches=SimpleNamespace(MAX_SCRIPTS=100), text_to_html=lambda title, body: body)
    asyncio.run(ns["_add_uploaded"]("set", "creator", "upload.txt", b"", TEXT))
    for call in ns["_new_page"].await_args_list:
        assert call.kwargs["import_warnings"] == script_import_warnings(split_scripts(TEXT))


@pytest.mark.parametrize("stored", [None, [], ["Template format differs"], '["Template format differs"]'])
def test_reopened_detail_restores_notice_independently_of_edited_body(stored):
    ns = persistence_namespace("script_doc_service.py", {"_detail"})
    ns.update(_color=lambda *args: "blue", sources_for=lambda doc: "", _all_checked=lambda *args: False, compliance_state=lambda doc: {"latest_check": {"score": 90}, "compliance_stale": True},
              sharedrev=SimpleNamespace(shared_id=lambda doc: None), batches=SimpleNamespace(missing_details=lambda details: []))
    doc = dict.fromkeys(["id", "title", "template_key", "status", "round", "created_by", "creator_name", "body_html",
                        "send_note", "decision_note", "decided_at", "updated_at", "created_at"])
    doc.update(body_text="The creator has edited this script", details={}, import_warnings=stored)
    detail = ns["_detail"](doc, [], None, "creator", None)
    assert detail.import_warnings == (json.loads(stored) if isinstance(stored, str) else stored or [])
    assert detail.latest_check == {"score": 90}
    assert detail.compliance_stale is True
