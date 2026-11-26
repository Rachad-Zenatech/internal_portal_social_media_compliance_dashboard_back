"""Regression checks for script-specific source import, persistence, checking and export."""
import asyncio
from io import BytesIO
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from uuid import uuid4
from xml.etree import ElementTree as ET
from zipfile import ZipFile

import pytest

from fastapi import HTTPException
from models.compliance.script_doc_model import DocSourcesUpdate
from services.compliance.evaluation import script_doc_sources as evidence
from services.compliance.evaluation import script_doc_service as core
from services.compliance.evaluation import script_doc_reads as reads
from services.compliance.evaluation import script_doc_pages as pages
from services.compliance.evaluation.script_split import split_scripts
from services.compliance.media.script_sources import extract, split
from services.compliance.media.script_export import script_docx, W
from services.compliance.media.document_layout import docx_to_text

TABLE = "| Time | Visual | Voice | Text |\n| 0:00 | Aircraft | Under development | Development |"
SOURCES = "Defense roles https://example.com/defense\nDevelopment status https://example.com/news\nOpening line is commentary."


def document(**changes):
    return {"id": uuid4(), "title": "Scale", "status": "DRAFT", "round": 0, "body_text": TABLE,
            "sources_text": SOURCES, "details": {}, **changes}


def test_extract_keeps_names_urls_and_notes_separate_from_script():
    body, raw = extract(TABLE + "\n| Sources used | " + SOURCES.replace("\n", "; ") + " |")
    assert body == TABLE
    assert raw == SOURCES
    wording, citations = split(evidence.export_text(document()))
    assert wording == TABLE
    assert "https://example.com/defense" in citations
    assert "https://example.com/news" in citations
    assert "commentary" in citations


def test_sources_for_supports_legacy_imports_and_explicit_removal():
    legacy = document(sources_text=None, body_text=TABLE + "\n| Sources used | Old https://example.com/old |")
    assert evidence.sources_for(legacy) == "Old https://example.com/old"
    legacy["sources_text"] = ""
    assert evidence.sources_for(legacy) == ""
    assert "example.com" not in evidence.export_text(legacy)


def test_each_bulk_import_stores_only_its_own_sources():
    first, second = uuid4(), uuid4()
    upload = f"Script 1: Scale\n{TABLE}\n| Sources used | First https://example.com/first |\nScript 2: Other\n{TABLE}\n| Sources used | Second https://example.com/second |"
    create = AsyncMock(side_effect=[first, second])
    with patch.object(pages.batches, "read_upload", AsyncMock(return_value=upload)), \
         patch.object(pages, "_pages", AsyncMock(side_effect=[[], [SimpleNamespace(id=first), SimpleNamespace(id=second)]])), \
         patch.object(pages, "_new_page", create), patch.object(pages, "log_audit_event", AsyncMock()):
        result = asyncio.run(pages._add_uploaded(uuid4(), uuid4(), "bulk.docx", b"test"))
    assert len(result) == 2
    for call, expected in zip(create.call_args_list, ("first", "second")):
        assert "Sources used" not in call.args[3]
        assert "Sources used" not in call.args[4]
        assert call.args[5] == f"{expected.title()} https://example.com/{expected}"


def test_check_passes_saved_sources_as_evidence_and_records_snapshot():
    doc = document()
    checker = AsyncMock(return_value={"score": 90})
    with patch.object(core, "get_access", AsyncMock(return_value=(doc, [], None))), patch.object(reads.scripts, "check_text", checker), patch.object(reads.compliance, "execute", AsyncMock()):
        result = asyncio.run(reads.check(doc["id"], uuid4(), TABLE + "\n| Sources used | Obsolete https://example.com/old |"))
    checked = checker.call_args.args[1]
    assert "example.com/old" not in checked
    assert "example.com/defense" in checked
    assert "Sources used" not in split(checked)[0]
    assert result["checked_script_sources"] == SOURCES


def test_save_enforces_edit_access_and_concurrent_change_protection():
    doc = document()
    req = DocSourcesUpdate(sources_text="Updated", expected_sources_text=SOURCES)
    access = SimpleNamespace(can_edit=False)
    with patch.object(core, "get_access", AsyncMock(return_value=(doc, [], access))), patch.object(evidence, "fetch_one", AsyncMock()) as write:
        with pytest.raises(HTTPException) as denied:
            asyncio.run(evidence.save(doc["id"], uuid4(), req))
        assert denied.value.status_code == 403
        write.assert_not_awaited()
    access.can_edit = True
    with patch.object(core, "get_access", AsyncMock(return_value=(doc, [], access))):
        with pytest.raises(HTTPException) as stale:
            asyncio.run(evidence.save(doc["id"], uuid4(), DocSourcesUpdate(sources_text="Updated", expected_sources_text="stale")))
        assert stale.value.status_code == 409


def test_save_updates_one_script_and_resets_review_checkins():
    doc = document(status="IN_REVIEW")
    req = DocSourcesUpdate(sources_text="Updated", expected_sources_text=SOURCES)
    with patch.object(core, "get_access", AsyncMock(return_value=(doc, [], SimpleNamespace(can_edit=True)))), \
         patch.object(evidence, "fetch_one", AsyncMock(return_value={"id": doc["id"]})) as write, \
         patch.object(core, "_event", AsyncMock()), patch.object(core, "note_change", AsyncMock()) as changed, \
         patch.object(core, "_broadcast_state", AsyncMock()), patch.object(core, "get", AsyncMock(return_value=doc)):
        asyncio.run(evidence.save(doc["id"], uuid4(), req))
    assert "WHERE id = $1" in write.call_args.args[0]
    assert write.call_args.args[1] == doc["id"]
    assert write.call_args.args[2] == "Updated"
    assert changed.await_count == 1


def test_export_restores_sources_as_merged_table_row_and_reimports():
    text = evidence.export_text(document())
    assert extract(text)[1] == SOURCES
    data = script_docx(text)
    with ZipFile(BytesIO(data)) as archive:
        root = ET.fromstring(archive.read("word/document.xml"))
    row = root.findall(f".//{{{W}}}tr")[-1]
    assert len(row) == 2
    assert [cell.find(f"{{{W}}}tcPr/{{{W}}}gridSpan").get(f"{{{W}}}val") for cell in row] == ["2", "2"]
    assert extract(docx_to_text(data))[1] == SOURCES


def test_bulk_roundtrip_does_not_split_sources_into_scripts():
    text = "Script 1: One\n" + evidence.export_text(document()) + "\nScript 2: Two\n" + evidence.export_text(document(sources_text="Different https://example.com/two"))
    scripts = split_scripts(text)
    assert len(scripts) == 2
    assert "example.com/two" not in extract(scripts[0][1])[1]
    assert extract(scripts[1][1])[1] == "Different https://example.com/two"


def test_shared_editor_export_uses_formatted_table_snapshot():
    doc = document(body_text="[0:00] Visual: Aircraft | Voiceover: Under development | On-screen: Development",
                   body_html="<table><tr><th>Time</th><th>Visual</th><th>Voice</th><th>Text</th></tr><tr><td>0:00</td><td>Aircraft</td><td>Under development</td><td>Development</td></tr></table>")
    exported = evidence.export_text(doc)
    assert "| Time | Visual | Voice | Text |" in exported
    assert "| 0:00 | Aircraft | Under development | Development |" in exported
    assert extract(exported)[1] == SOURCES
