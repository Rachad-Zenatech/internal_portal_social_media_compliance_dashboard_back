"""Comments on an area of an image: the box must be whole, inside the image, and on one of the post's own images."""

import unittest
from unittest.mock import AsyncMock, patch
from uuid import uuid4

from fastapi import HTTPException
from pydantic import ValidationError

from models.compliance.submission_model import ReviewCommentCreate
from services.compliance.submissions import submission_service as subs

SUB = uuid4()
USER = uuid4()
FILE = uuid4()
BOX = {"file_id": FILE, "region_x": 0.1, "region_y": 0.2, "region_w": 0.3, "region_h": 0.25}


class TheBox(unittest.TestCase):
    def test_a_general_comment_has_no_box(self):
        c = ReviewCommentCreate(body="Looks fine")
        self.assertIsNone(c.file_id)

    def test_a_whole_box_is_accepted(self):
        self.assertEqual(ReviewCommentCreate(body="Move the logo", **BOX).region_w, 0.3)

    def test_a_box_missing_a_part_is_refused(self):
        for part in ("file_id", "region_x", "region_y", "region_w", "region_h"):
            box = {k: v for k, v in BOX.items() if k != part}
            with self.assertRaises(ValidationError, msg=part):
                ReviewCommentCreate(body="x", **box)

    def test_a_box_outside_the_image_is_refused(self):
        with self.assertRaises(ValidationError):
            ReviewCommentCreate(body="x", **{**BOX, "region_x": 0.9, "region_w": 0.3})
        with self.assertRaises(ValidationError):
            ReviewCommentCreate(body="x", **{**BOX, "region_y": -0.1})

    def test_an_empty_box_is_refused(self):
        with self.assertRaises(ValidationError):
            ReviewCommentCreate(body="x", **{**BOX, "region_w": 0})

    def test_a_comment_is_on_an_image_area_or_a_video_moment_not_both(self):
        with self.assertRaises(ValidationError):
            ReviewCommentCreate(body="x", start_sec=3, **BOX)


class SavingIt(unittest.IsolatedAsyncioTestCase):
    async def add(self, image_found):
        row = {"created_by": uuid4(), "status": "IN_REVIEW", "current_version": 1}
        calls = []

        async def fetch_one(sql, *args):
            calls.append((sql, args))
            if "FROM content_submission_files" in sql:
                return {"id": FILE} if image_found else None
            return {"id": uuid4()}

        with patch.object(subs, "_comment_access", new=AsyncMock(return_value=row)), \
             patch.object(subs, "_can_review", new=AsyncMock(return_value=True)), \
             patch.object(subs, "_is_super_admin", new=AsyncMock(return_value=False)), \
             patch.object(subs, "fetch_one", new=fetch_one), \
             patch.object(subs, "_comment_out", new=lambda *a, **k: "saved"):
            result = await subs.add_comment(SUB, USER, ReviewCommentCreate(body="Move the logo", **BOX))
        return result, calls

    async def test_the_box_is_saved_with_the_comment(self):
        result, calls = await self.add(True)
        self.assertEqual(result, "saved")
        insert = next(args for sql, args in calls if "INSERT INTO content_review_comments" in sql)
        self.assertIn(FILE, insert)
        self.assertIn(0.3, insert)

    async def test_an_image_from_another_post_is_refused(self):
        with self.assertRaises(HTTPException) as caught:
            await self.add(False)
        self.assertEqual(caught.exception.status_code, 422)


if __name__ == "__main__":
    unittest.main()
