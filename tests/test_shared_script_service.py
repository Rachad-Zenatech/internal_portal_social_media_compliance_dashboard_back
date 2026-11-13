"""Shared documents for script pages: reading an uploaded script's text into details and beats, and staying out of the way
when the shared editor is off or the shared server is down."""

import unittest
from unittest.mock import AsyncMock, patch
from uuid import uuid4

from services.compliance.evaluation import shared_script_service as shared

UPLOADED = """## 1. Twenty five years in software
**Storyline:** Operator's receipt (his own experience)
Hook score 80.8 (strong) | about 35 seconds | Green lane
| Time | Visual / footage | Voiceover | On-screen text |
| --- | --- | --- | --- |
| 0:00 | Laptop with code | Twenty five years in software didn't prepare me for drones. | **25 YEARS IN SOFTWARE** |
| 0:10 | Drone on a test pad | In an aircraft, a bug costs you the aircraft. |
Footage and compliance note: Use stock footage only.
"""


class TextToBeats(unittest.TestCase):
    def test_table_rows_become_beats_and_facts_become_details(self):
        details, beats = shared.text_to_beats(UPLOADED)
        self.assertEqual(details["Storyline"], "Operator's receipt (his own experience)")
        self.assertEqual((details["Hook score"], details["Duration"], details["Lane"]), ("80.8 (strong)", "about 35 seconds", "Green lane"))
        self.assertEqual(details["Description"], "Use stock footage only.")
        self.assertEqual(len(beats), 2)
        self.assertEqual(beats[0]["onscreen"], "25 YEARS IN SOFTWARE")
        self.assertEqual(beats[1]["onscreen"], "")

    def test_a_script_without_a_table_gets_a_beat_per_line(self):
        _, beats = shared.text_to_beats("My script\nFirst line.\nSecond line.")
        self.assertEqual([b["voiceover"] for b in beats], ["First line.", "Second line."])


class Enabled(unittest.IsolatedAsyncioTestCase):
    async def test_off_by_default(self):
        with patch.dict("os.environ", {"SCRIPT_EDITOR": "legacy"}), patch.object(shared, "_page", new=AsyncMock()) as page:
            self.assertIsNone(await shared.ensure_shared_document(uuid4()))
            page.assert_not_awaited()

    async def test_a_down_server_never_breaks_page_creation(self):
        row = {"id": uuid4(), "title": "T", "body_text": "", "share_document_id": None, "position": 3, "email": "a@b.com", "set_title": "S"}
        with patch.dict("os.environ", {"SCRIPT_EDITOR": "shared"}), patch.object(shared, "_page", new=AsyncMock(return_value=row)), \
                patch.object(shared, "call", new=AsyncMock(side_effect=shared.SharedScriptError("down"))):
            self.assertIsNone(await shared.ensure_shared_document(row["id"]))

    async def test_made_once_and_remembered(self):
        row = {"id": uuid4(), "title": "T", "body_text": UPLOADED, "share_document_id": None, "position": 3, "email": "a@b.com", "set_title": "S"}
        after = {**row, "share_document_id": "doc-social-script-1"}
        call = AsyncMock(return_value={"document_id": "doc-social-script-1"})
        with patch.dict("os.environ", {"SCRIPT_EDITOR": "shared"}), patch.object(shared, "_page", new=AsyncMock(side_effect=[row, after])), \
                patch.object(shared, "call", new=call), patch.object(shared, "execute", new=AsyncMock()):
            self.assertEqual(await shared.ensure_shared_document(row["id"]), "doc-social-script-1")
        sent = call.await_args.kwargs["json"]
        self.assertEqual(sent["user_email"], "a@b.com")
        self.assertEqual(len(sent["rows"]), 2)
        self.assertEqual(sent["number"], 3)
        self.assertEqual(sent["external_ref"]["id"], str(row["id"]))


class DeleteShared(unittest.IsolatedAsyncioTestCase):
    async def test_a_whole_file_is_one_call_per_creator(self):
        rows = [{"share_document_id": f"doc-{i}", "email": "a@b.com"} for i in range(10)]
        call = AsyncMock(return_value={"deleted": [r["share_document_id"] for r in rows], "skipped": []})
        with patch.dict("os.environ", {"SCRIPT_EDITOR": "shared"}), patch.object(shared, "fetch_all", new=AsyncMock(return_value=rows)), \
                patch.object(shared, "call", new=call):
            await shared.delete_shared_documents(set_id=uuid4())
        call.assert_awaited_once()
        self.assertEqual(len(call.await_args.kwargs["json"]["document_ids"]), 10)
        self.assertEqual(call.await_args.kwargs["json"]["actor_email"], "a@b.com")

    async def test_a_down_server_never_blocks_a_delete(self):
        rows = [{"share_document_id": "doc-1", "email": "a@b.com"}]
        with patch.dict("os.environ", {"SCRIPT_EDITOR": "shared"}), patch.object(shared, "fetch_all", new=AsyncMock(return_value=rows)), \
                patch.object(shared, "call", new=AsyncMock(side_effect=shared.SharedScriptError("down"))):
            await shared.delete_shared_documents(page_ids=[uuid4()])

    async def test_off_when_the_shared_editor_is_off(self):
        with patch.dict("os.environ", {"SCRIPT_EDITOR": "legacy"}), patch.object(shared, "fetch_all", new=AsyncMock()) as q:
            await shared.delete_shared_documents(page_ids=[uuid4()])
            q.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
