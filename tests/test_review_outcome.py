import sqlite3
import unittest
from services.compliance.submissions.review_outcome import status_sql

class ReviewOutcomeTests(unittest.TestCase):
    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        self.addCleanup(self.conn.close)
        self.conn.execute("CREATE TABLE posts (id TEXT, status TEXT, publish_after_changes BOOLEAN)")
        self.conn.executemany("INSERT INTO posts VALUES (?,?,?)", [
            ("held", "IN_REVIEW", True), ("final", "CHANGES_REQUESTED", True),
            ("back", "CHANGES_REQUESTED", False), ("review", "IN_REVIEW", False),
            ("legacy", "APPROVED", False), ("draft", "DRAFT", True)])

    def members(self, status):
        return {row[0] for row in self.conn.execute(f"SELECT id FROM posts s WHERE {status_sql()} = ?", (status,))}

    def test_approved_filter_includes_approved_final_edits_and_open_suggestions(self):
        self.assertEqual(self.members("APPROVED"), {"held", "final", "legacy"})

    def test_needs_changes_excludes_approved_posts(self):
        self.assertEqual(self.members("CHANGES_REQUESTED"), {"back"})
        self.assertEqual(self.members("IN_REVIEW"), {"review"})

    def test_old_permission_flag_does_not_promote_drafts(self):
        self.assertEqual(self.members("DRAFT"), {"draft"})
