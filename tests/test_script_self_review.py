import asyncio
import os
import unittest
from unittest.mock import AsyncMock, patch
from uuid import uuid4

from services.compliance.evaluation import script_review_service as scripts

DEV_ON = {"ALLOW_SUPER_ADMIN_SELF_REVIEW": "true", "APP_ENV": "development"}
DEV_OFF = {"ALLOW_SUPER_ADMIN_SELF_REVIEW": "", "APP_ENV": "development"}


class ScriptSelfReviewTests(unittest.TestCase):
    """A script sent only to its own writer would sit in review for good: nobody else sees it, and they can't approve it."""

    def test_sent_only_to_the_creator_is_refused(self):
        me = uuid4()
        self.assertTrue(scripts.only_sent_to_self([me], me))
        self.assertTrue(scripts.only_sent_to_self([str(me)], me))

    def test_another_reviewer_alongside_the_creator_is_fine(self):
        me = uuid4()
        self.assertFalse(scripts.only_sent_to_self([me, uuid4()], me))
        self.assertFalse(scripts.only_sent_to_self([uuid4()], me))

    def test_no_pick_is_left_to_the_required_field_check(self):
        self.assertFalse(scripts.only_sent_to_self([], uuid4()))
        self.assertFalse(scripts.only_sent_to_self(None, uuid4()))


class SelfReviewInDevelopmentTests(unittest.TestCase):
    """In development a super admin may review and approve their own scripts; never in production."""

    def may(self, env, is_super):
        with patch.dict(os.environ, env), \
             patch.object(scripts.subs, "_is_super_admin", new=AsyncMock(return_value=is_super)):
            return asyncio.run(scripts.may_review_own_script(uuid4()))

    def test_super_admin_in_development_with_the_flag(self):
        self.assertTrue(self.may(DEV_ON, True))

    def test_never_in_production_even_with_the_flag(self):
        self.assertFalse(self.may({"ALLOW_SUPER_ADMIN_SELF_REVIEW": "true", "APP_ENV": "production"}, True))

    def test_not_without_the_flag(self):
        self.assertFalse(self.may(DEV_OFF, True))

    def test_not_for_other_reviewers(self):
        self.assertFalse(self.may(DEV_ON, False))

    def test_the_creator_may_approve_only_when_switched_on(self):
        me = uuid4()
        row = {"created_by": me}
        for env, expected in ((DEV_ON, True), (DEV_OFF, False)):
            with patch.dict(os.environ, env), \
                 patch.object(scripts.subs, "_can_review", new=AsyncMock(return_value=True)), \
                 patch.object(scripts.subs, "_is_super_admin", new=AsyncMock(return_value=True)):
                self.assertEqual(asyncio.run(scripts._reviewer_may_act(row, me)), expected)


if __name__ == "__main__":
    unittest.main()
