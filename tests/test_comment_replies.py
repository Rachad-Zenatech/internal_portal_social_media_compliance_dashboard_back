"""Replying to a review comment, as in a chat app: the reply quotes the comment it answers."""
import unittest
from datetime import datetime, timezone
from unittest.mock import AsyncMock, patch
from uuid import uuid4

from fastapi import HTTPException

from models.compliance.submission_model import ReviewCommentCreate
from services.compliance.submissions import submission_service as subs

SUB, USER, REVIEWER, PARENT = uuid4(), uuid4(), uuid4(), uuid4()
NOW = datetime(2026, 11, 12, 15, tzinfo=timezone.utc)


def stored(**over):
    row = {
        "id": uuid4(), "submission_id": SUB, "version_number": 1, "category": "NOTE", "body": "Agreed, cut it.",
        "start_sec": None, "end_sec": None, "author_id": USER, "author_name": "Test Suggester", "from_reviewer": True,
        "resolved_at": None, "resolved_by_name": None, "created_at": NOW, "submission_created_by": uuid4(),
        "reply_to_id": None, "reply_to_author": None, "reply_to_body": None,
    }
    row.update(over)
    return row


class ReplyShownWithItsQuote(unittest.TestCase):
    def test_a_reply_carries_who_and_what_it_answers(self):
        out = subs._comment_out(stored(reply_to_id=PARENT, reply_to_author="Test Reviewer", reply_to_body="Cut 0:12?"), USER, False)
        self.assertEqual((out.reply_to_id, out.reply_to_author, out.reply_to_body), (PARENT, "Test Reviewer", "Cut 0:12?"))

    def test_a_reply_to_a_deleted_comment_keeps_the_link_but_no_text(self):
        out = subs._comment_out(stored(reply_to_id=PARENT, reply_to_author="Test Reviewer", reply_to_body=None), USER, False)
        self.assertEqual(out.reply_to_id, PARENT)
        self.assertIsNone(out.reply_to_body)

    def test_a_plain_comment_has_no_quote(self):
        self.assertIsNone(subs._comment_out(stored(), USER, False).reply_to_id)


class ReplyMustBeToThisPost(unittest.IsolatedAsyncioTestCase):
    async def add(self, parent_exists):
        row = {"id": SUB, "created_by": REVIEWER, "status": "IN_REVIEW", "current_version": 1}
        lookups = [{"?column?": 1} if parent_exists else None, {"id": uuid4()}, stored(reply_to_id=PARENT)]
        fetch = AsyncMock(side_effect=lookups)
        with patch.object(subs, "_comment_access", new=AsyncMock(return_value=row)), \
             patch.object(subs, "_can_review", new=AsyncMock(return_value=False)), \
             patch.object(subs, "_is_super_admin", new=AsyncMock(return_value=False)), \
             patch.object(subs, "fetch_one", new=fetch):
            out = await subs.add_comment(SUB, USER, ReviewCommentCreate(body="Agreed, cut it.", reply_to_id=PARENT))
        return out, fetch

    async def test_a_reply_to_a_comment_on_this_post_is_saved_with_the_link(self):
        out, fetch = await self.add(parent_exists=True)
        self.assertEqual(out.reply_to_id, PARENT)
        insert_args = fetch.await_args_list[1].args
        self.assertIn("reply_to_id", insert_args[0])
        self.assertIn(PARENT, insert_args)

    async def test_a_reply_to_a_missing_or_other_posts_comment_is_refused(self):
        with self.assertRaises(HTTPException) as caught:
            await self.add(parent_exists=False)
        self.assertEqual(caught.exception.status_code, 422)


if __name__ == "__main__":
    unittest.main()
