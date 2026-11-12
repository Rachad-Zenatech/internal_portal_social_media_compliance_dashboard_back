"""A suggesting reviewer: sees what is sent to them by name, comments and suggests edits to scripts, can't decide."""

import unittest
from unittest.mock import AsyncMock, patch
from uuid import uuid4

from fastapi import HTTPException

from models.compliance.script_review_model import ScriptSuggestionRequest, ScriptVersion
from services.compliance.evaluation import script_review_service as srs
from services.compliance.submissions import submission_service as subs

USER = uuid4()
SUB = uuid4()
AUTH = "services.shared.auth.auth_service.user_can_access_page"


def role(can_review=False, can_manage=False, suggest_permission=False, super_admin=False):
    """Patches for one person's permissions."""

    async def access(user_id, page, action="VIEW"):
        return super_admin or (page, action) == ("CONTENT_REVIEW", "SUGGEST") and suggest_permission or \
            (page, action) == ("CONTENT_REVIEW", "APPROVE") and can_review or \
            (page, action) == ("CONTENT_SUBMISSIONS", "DELETE") and can_manage

    return [
        patch(AUTH, new=access),
        patch.object(subs, "_is_super_admin", new=AsyncMock(return_value=super_admin)),
    ]


class WhoIsASuggester(unittest.IsolatedAsyncioTestCase):
    async def check(self, **who):
        patches = role(**who)
        for p in patches:
            p.start()
        try:
            return await subs._is_suggester(USER), await subs._can_suggest(USER), await subs._can_review(USER)
        finally:
            for p in patches:
                p.stop()

    async def test_someone_with_only_the_suggest_permission_is_a_suggester(self):
        self.assertEqual(await self.check(suggest_permission=True), (True, True, False))

    async def test_a_reviewer_can_suggest_but_is_not_only_a_suggester(self):
        self.assertEqual(await self.check(can_review=True), (False, True, True))

    async def test_a_reviewer_who_also_has_the_suggest_permission_is_not_a_suggester(self):
        self.assertFalse((await self.check(can_review=True, suggest_permission=True))[0])

    async def test_a_content_manager_is_not_a_suggester(self):
        self.assertFalse((await self.check(can_manage=True, suggest_permission=True))[0])

    async def test_a_super_admin_is_not_a_suggester(self):
        self.assertFalse((await self.check(super_admin=True))[0])

    async def test_someone_with_no_review_permission_cannot_suggest(self):
        self.assertEqual(await self.check(), (False, False, False))


class WhatASuggesterSees(unittest.IsolatedAsyncioTestCase):
    async def test_they_work_on_no_brand_so_only_what_is_sent_to_them_by_name_shows(self):
        with patch.object(subs, "_is_suggester", new=AsyncMock(return_value=True)):
            self.assertEqual(await subs._brand_scope(USER), [])

    async def test_they_can_read_other_peoples_content(self):
        patches = role(suggest_permission=True)
        for p in patches:
            p.start()
        try:
            self.assertTrue(await subs._can_see_team_content(USER))
        finally:
            for p in patches:
                p.stop()

    async def test_someone_sent_nothing_by_name_cannot_open_a_post_on_a_brand_they_do_not_work_on(self):
        row = {"id": SUB, "created_by": uuid4(), "brand_codes": ["ZENADRONE"], "brand_code": "ZENADRONE", "reviewer_ids": []}
        with patch.object(subs, "fetch_one", new=AsyncMock(return_value=row)), \
             patch.object(subs, "_is_super_admin", new=AsyncMock(return_value=False)), \
             patch.object(subs, "_can_see_team_content", new=AsyncMock(return_value=True)), \
             patch.object(subs, "_brand_scope", new=AsyncMock(return_value=[])):
            with self.assertRaises(HTTPException) as caught:
                await subs._get_owned_row(SUB, USER, manager_can_read=True)
        self.assertEqual(caught.exception.status_code, 404)

    async def test_a_post_sent_to_them_by_name_opens(self):
        row = {"id": SUB, "created_by": uuid4(), "brand_codes": ["ZENADRONE"], "brand_code": "ZENADRONE", "reviewer_ids": [USER]}
        with patch.object(subs, "fetch_one", new=AsyncMock(return_value=row)), \
             patch.object(subs, "_is_super_admin", new=AsyncMock(return_value=False)), \
             patch.object(subs, "_can_see_team_content", new=AsyncMock(return_value=True)), \
             patch.object(subs, "_brand_scope", new=AsyncMock(return_value=[])):
            self.assertEqual((await subs._get_owned_row(SUB, USER, manager_can_read=True))["id"], SUB)


class WhatASuggesterMayDo(unittest.IsolatedAsyncioTestCase):
    async def comment_access(self, can_suggest):
        row = {"id": SUB, "created_by": uuid4()}
        with patch.object(subs, "_get_owned_row", new=AsyncMock(return_value=row)), \
             patch.object(subs, "_can_suggest", new=AsyncMock(return_value=can_suggest)):
            return await subs._comment_access(SUB, USER)

    async def test_they_can_comment(self):
        self.assertEqual((await self.comment_access(True))["id"], SUB)

    async def test_someone_who_cannot_suggest_cannot_comment_on_someone_elses_post(self):
        with self.assertRaises(HTTPException) as caught:
            await self.comment_access(False)
        self.assertEqual(caught.exception.status_code, 403)


def version(body="Open on the hangar."):
    return ScriptVersion(version_number=1, body=body, source="UPLOAD", created_at="2026-11-12T10:00:00Z")


class SuggestingOnAScript(unittest.IsolatedAsyncioTestCase):
    async def suggestable(self, *, suggester=True, script_status="IN_REVIEW", versions=(version(),)):
        row = {"id": SUB, "created_by": uuid4(), "script_status": script_status, "script_required": True, "content_type": "VIDEO"}
        with patch.object(srs.subs, "_get_owned_row", new=AsyncMock(return_value=row)), \
             patch.object(srs.subs, "_is_suggester", new=AsyncMock(return_value=suggester)), \
             patch.object(srs, "_versions", new=AsyncMock(return_value=list(versions))):
            return await srs._suggestable(SUB, USER)

    async def test_a_suggester_can_suggest_on_the_script_in_review(self):
        _, sent = await self.suggestable()
        self.assertEqual(sent.version_number, 1)

    async def test_only_a_suggester_suggests_reviewers_edit_the_script_themselves(self):
        with self.assertRaises(HTTPException) as caught:
            await self.suggestable(suggester=False)
        self.assertEqual(caught.exception.status_code, 403)

    async def test_nothing_can_be_suggested_once_the_script_is_not_in_review(self):
        for status in ("APPROVED", "CHANGES_REQUESTED", "DRAFT"):
            with self.assertRaises(HTTPException) as caught:
                await self.suggestable(script_status=status)
            self.assertEqual(caught.exception.status_code, 409, status)

    async def test_a_script_nobody_sent_has_nothing_to_suggest_on(self):
        with self.assertRaises(HTTPException) as caught:
            await self.suggestable(versions=())
        self.assertEqual(caught.exception.status_code, 409)

    async def test_the_suggestion_is_kept_apart_from_the_script_and_the_same_wording_with_no_notes_is_nothing(self):
        executed = []

        async def execute(sql, *args):
            executed.append(sql)

        with patch.object(srs, "_suggestable", new=AsyncMock(return_value=({}, version("Same wording")))), \
             patch.object(srs, "execute", new=execute), \
             patch.object(srs, "get_stage", new=AsyncMock(return_value="stage")):
            await srs.save_suggestion(SUB, USER, ScriptSuggestionRequest(text="Same  wording\n"))
            await srs.save_suggestion(SUB, USER, ScriptSuggestionRequest(text="Different wording"))
        self.assertTrue(executed[0].lstrip().startswith("DELETE FROM content_script_suggestions"))
        self.assertIn("INSERT INTO content_script_suggestions", executed[1])
        # Whatever they save, the script's own versions are never touched.
        self.assertFalse(any("content_script_versions" in sql for sql in executed))


if __name__ == "__main__":
    unittest.main()
