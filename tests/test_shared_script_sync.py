"""Following the Share Document server's script events: the page's text and status are brought up to date, once."""

import unittest
from unittest.mock import AsyncMock, patch
from uuid import uuid4

from services.compliance.evaluation import shared_script_sync as sync


def page(status="IN_REVIEW"):
    return {"id": uuid4(), "title": "T", "status": status, "created_by": uuid4(), "email": "a@b.com"}


class Handle(unittest.IsolatedAsyncioTestCase):
    async def test_an_unknown_document_is_ignored(self):
        with patch.object(sync, "_page", new=AsyncMock(return_value=None)), patch.object(sync, "refresh_text", new=AsyncMock()) as r:
            await sync.handle({"event": "merge.completed", "documentId": "x"})
            r.assert_not_awaited()

    async def test_a_merge_reads_the_text_again_and_follows_the_cleared_check_ins(self):
        with patch.object(sync, "_page", new=AsyncMock(return_value=page())), patch.object(sync, "refresh_text", new=AsyncMock()) as r,                 patch.object(sync, "_mirror_checkins", new=AsyncMock()) as c, patch.object(sync, "_mirror_status", new=AsyncMock()) as m:
            await sync.handle({"event": "merge.completed", "documentId": "x"})
            r.assert_awaited_once()
            c.assert_awaited_once()
            m.assert_not_awaited()

    async def test_check_ins_the_shared_review_cleared_are_cleared_here(self):
        state = {"collaborators": [{"email": "A@b.com", "vote": None}, {"email": "c@d.com", "vote": "APPROVE"}]}
        run = AsyncMock()
        with patch.object(sync.shared, "call", new=AsyncMock(return_value=state)), patch.object(sync, "execute", new=run):
            await sync._mirror_checkins(page(), "x")
        self.assertEqual(run.await_args.args[2], ["a@b.com"])

    async def test_approval_reads_the_text_then_mirrors_the_status(self):
        calls = []
        with patch.object(sync, "_page", new=AsyncMock(return_value=page())), \
                patch.object(sync, "refresh_text", new=AsyncMock(side_effect=lambda *a: calls.append("text"))), \
                patch.object(sync, "_mirror_status", new=AsyncMock(side_effect=lambda *a: calls.append("status"))):
            await sync.handle({"event": "review.decided", "documentId": "x", "status": "APPROVED"})
        self.assertEqual(calls, ["text", "status"])

    async def test_a_changes_request_only_mirrors_the_status(self):
        with patch.object(sync, "_page", new=AsyncMock(return_value=page())), patch.object(sync, "refresh_text", new=AsyncMock()) as r, \
                patch.object(sync, "_mirror_status", new=AsyncMock()) as m:
            await sync.handle({"event": "review.finalized", "documentId": "x", "status": "CHANGES_REQUESTED"})
            r.assert_not_awaited()
            m.assert_awaited_once()

    async def test_a_failure_is_swallowed(self):
        with patch.object(sync, "_page", new=AsyncMock(side_effect=RuntimeError("db"))):
            await sync.handle({"event": "merge.completed", "documentId": "x"})

    async def test_a_repeated_status_does_nothing_twice(self):
        pages = AsyncMock()
        with patch.object(sync, "fetch_one", new=AsyncMock(return_value=None)), \
                patch("services.compliance.evaluation.script_doc_pages._approved", new=pages):
            await sync._mirror_status(page(), {"status": "APPROVED", "round": 1})
        pages.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
