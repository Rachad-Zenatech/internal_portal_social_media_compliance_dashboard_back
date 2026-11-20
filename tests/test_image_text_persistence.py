"""Exercise image status persistence against PostgreSQL's real parameter typing."""
import os
import unittest
from unittest.mock import patch
from uuid import uuid4

from postgresql_db import database
from services.compliance.media import image_text_service


@unittest.skipUnless(os.getenv("DATABASE_URL"), "Requires PostgreSQL")
class ImageTextPersistenceTests(unittest.IsolatedAsyncioTestCase):
    async def test_completed_and_failed_statuses_save_with_varchar_column(self):
        pool = await database.create_pool()
        try:
            async with pool.acquire() as conn:
                async with conn.transaction():
                    await conn.execute("""
                        CREATE TEMP TABLE content_submission_files (
                            id UUID PRIMARY KEY, image_text_status VARCHAR(20),
                            image_text TEXT, image_text_error TEXT, person_detected BOOLEAN,
                            face_boxes JSONB DEFAULT '[]'::jsonb,
                            face_detection_version INTEGER DEFAULT 0,
                            image_text_updated_at TIMESTAMPTZ
                        ) ON COMMIT DROP
                    """)
                    file_id = uuid4()
                    await conn.execute("INSERT INTO content_submission_files (id) VALUES ($1)", file_id)
                    with patch.object(image_text_service, "get_pool", return_value=conn):
                        for status, text, error, version in (
                            ("FAILED", None, "model unavailable", 0),
                            ("COMPLETED", "Image headline", None, 2),
                        ):
                            with self.subTest(status=status):
                                await image_text_service._save(file_id, status, text=text, error=error)
                                row = await conn.fetchrow("SELECT * FROM content_submission_files WHERE id=$1", file_id)
                                self.assertEqual(row["image_text_status"], status)
                                self.assertEqual(row["image_text"], text)
                                self.assertEqual(row["image_text_error"], error)
                                self.assertEqual(row["face_detection_version"], version)
                                self.assertIsNotNone(row["image_text_updated_at"])
        finally:
            await database.close_pool()
