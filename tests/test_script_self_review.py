import asyncio
import unittest
from unittest.mock import AsyncMock, patch
from uuid import uuid4

from services.compliance.evaluation import script_review_service as scripts


class ScriptSelfReviewTests(unittest.TestCase):
    """A script sent only to its own writer would sit in review for good unless they may approve it themselves."""

    def test_sent_only_to_the_creator(self):
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


class SuperAdminsReviewTheirOwnScriptsTests(unittest.TestCase):
    """A super admin may review their own scripts (as with posts); nobody else may."""

    def may(self, is_super):
        with patch.object(scripts.subs, "_is_super_admin", new=AsyncMock(return_value=is_super)):
            return asyncio.run(scripts.may_review_own_script(uuid4()))

    def test_a_super_admin_may(self):
        self.assertTrue(self.may(True))

    def test_anyone_else_may_not(self):
        self.assertFalse(self.may(False))


if __name__ == "__main__":
    unittest.main()
