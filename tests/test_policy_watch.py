"""Policy watch: platform content-policy norms kept up to date from the platforms' policy pages, approved by super
admins only."""
import unittest
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

from fastapi import HTTPException

from services.compliance.evaluation import context_service
from services.compliance.evaluation import policy_watch_service as pw

ADMIN, OTHER, ENTRY, PROPOSAL = uuid4(), uuid4(), uuid4(), uuid4()
LONG = "\n".join(f"Policy line {i}: what is and isn't allowed on the platform for business posts." for i in range(40))


class ReadingPolicyPages(unittest.TestCase):
    def test_links_are_taken_from_the_source(self):
        src = "https://a.example/one · https://b.example/two?hl=en"
        self.assertEqual(pw.urls_of(src), ["https://a.example/one", "https://b.example/two?hl=en"])

    def test_small_edits_are_noise(self):
        self.assertIsNone(pw.diff(LONG, LONG.replace("line 3:", "line 3 (updated):")))

    def test_a_new_section_is_a_change_listing_what_was_added(self):
        added = "\n".join(f"New rule {i}: label AI-generated product footage clearly in every promotional post." for i in range(5))
        out = pw.diff(LONG, LONG + "\n" + added)
        self.assertIsNotNone(out)
        self.assertIn("+ New rule 0", out)


class ReadOnePage(unittest.IsolatedAsyncioTestCase):
    async def read(self, html, before=None):
        http = MagicMock()
        response = MagicMock(text=html)
        response.raise_for_status = MagicMock()
        http.get = AsyncMock(return_value=response)
        counts = {"added": 0, "updated": 0, "unchanged": 0, "skipped": 0, "failed": 0}
        store = AsyncMock(side_effect=[before, {"url": "u"}])
        with patch.object(pw, "fetch_one", new=store):
            changed = await pw._read(http, "https://p.example", counts)
        return changed, counts, store

    async def test_a_page_that_needs_a_browser_is_not_watched(self):
        changed, counts, store = await self.read("<html><body><div id='app'></div>Loading…</body></html>")
        self.assertIsNone(changed)
        self.assertEqual(counts["failed"], 1)
        self.assertIn(pw.NEEDS_BROWSER, store.await_args_list[1].args)

    async def test_the_first_read_is_only_stored(self):
        changed, counts, _ = await self.read("<p>" + LONG.replace("\n", "</p><p>") + "</p>")
        self.assertIsNone(changed)
        self.assertEqual(counts["added"], 1)

    async def test_a_changed_page_returns_what_changed(self):
        new = LONG + "\n" + "\n".join(f"Brand new requirement {i} for paid partnership disclosure on every post." for i in range(5))
        before = {"text_hash": "old", "page_text": LONG}
        changed, counts, _ = await self.read("<p>" + new.replace("\n", "</p><p>") + "</p>", before)
        self.assertIn("Brand new requirement", changed)


class OnlySuperAdmins(unittest.IsolatedAsyncioTestCase):
    async def test_others_cannot_see_or_decide_on_updates(self):
        with patch.object(pw, "fetch_one", new=AsyncMock(return_value=None)):
            for call in (pw.overview(OTHER), pw.check_now(OTHER), pw.approve(PROPOSAL, OTHER), pw.reject(PROPOSAL, OTHER)):
                with self.assertRaises(HTTPException) as caught:
                    await call
                self.assertEqual(caught.exception.status_code, 403)

    async def test_others_cannot_edit_a_content_policy_norm(self):
        with patch.object(context_service, "fetch_one", new=AsyncMock(return_value=None)):
            with self.assertRaises(HTTPException) as caught:
                await context_service._require_super_admin_for_policy({"seed_key": "policy:META"}, OTHER)
        self.assertEqual(caught.exception.status_code, 403)

    async def test_other_norms_stay_editable_by_their_usual_editors(self):
        with patch.object(context_service, "fetch_one", new=AsyncMock(return_value=None)) as lookup:
            await context_service._require_super_admin_for_policy({"seed_key": "platform:LINKEDIN"}, OTHER)
        lookup.assert_not_awaited()


class Approving(unittest.IsolatedAsyncioTestCase):
    def pending(self, status="PENDING"):
        return {
            "id": PROPOSAL, "context_id": ENTRY, "norm_title": "LinkedIn content policy", "platforms": ["LINKEDIN"],
            "source_urls": ["https://www.linkedin.com/legal/ads-policy"], "changes": '["Ads now need X"]',
            "current_body": "Old", "current_do_examples": None, "current_dont_examples": None, "body": "New wording",
            "do_examples": "Do this", "dont_examples": "Not that", "status": status,
            "created_at": datetime(2026, 11, 12, tzinfo=timezone.utc),
        }

    async def test_an_already_decided_update_cannot_be_approved(self):
        with patch.object(pw, "require_super_admin", new=AsyncMock()), \
             patch.object(pw, "fetch_one", new=AsyncMock(return_value=self.pending("REJECTED"))):
            with self.assertRaises(HTTPException) as caught:
                await pw.approve(PROPOSAL, ADMIN)
        self.assertEqual(caught.exception.status_code, 409)

    async def test_approving_writes_the_super_admins_edits_to_the_norm(self):
        conn = MagicMock()
        conn.fetchval = AsyncMock(return_value=ENTRY)
        conn.execute = AsyncMock()
        conn.transaction = MagicMock(return_value=AsyncMock())
        pool = MagicMock()
        pool.acquire = MagicMock(return_value=AsyncMock(__aenter__=AsyncMock(return_value=conn)))
        with patch.object(pw, "require_super_admin", new=AsyncMock()), \
             patch.object(pw, "fetch_one", new=AsyncMock(return_value=self.pending())), \
             patch.object(pw, "get_pool", new=MagicMock(return_value=pool)), \
             patch.object(pw, "log_audit_event", new=AsyncMock()), \
             patch.object(pw.reference_index_service, "sync_soon"), \
             patch.object(pw, "overview", new=AsyncMock(return_value="overview")):
            from models.compliance.context_model import PolicyProposalApprove
            await pw.approve(PROPOSAL, ADMIN, PolicyProposalApprove(body="Edited wording"))
        args = conn.fetchval.await_args.args
        self.assertEqual(args[1:5], (ENTRY, "Edited wording", "Do this", "Not that"))


if __name__ == "__main__":
    unittest.main()
