import unittest
from uuid import uuid4

from services.compliance.evaluation import script_review_service as scripts


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


if __name__ == "__main__":
    unittest.main()
