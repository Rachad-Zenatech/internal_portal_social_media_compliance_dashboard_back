import sqlite3
import unittest
from services.compliance.submissions.review_outcome import status_sql, ACTIVITY_ACTIONS

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

    def test_approved_filter_only_includes_released_approvals(self):
        self.assertEqual(self.members("APPROVED"), {"final", "legacy"})

    def test_needs_changes_excludes_approved_posts(self):
        self.assertEqual(self.members("CHANGES_REQUESTED"), {"back"})
        self.assertEqual(self.members("IN_REVIEW"), {"held", "review"})

    def test_old_permission_flag_does_not_promote_drafts(self):
        self.assertEqual(self.members("DRAFT"), {"draft"})

    def test_timer_release_moves_final_edits_into_approved(self):
        self.conn.execute("UPDATE posts SET status='CHANGES_REQUESTED' WHERE id='held'")
        self.assertIn("held", self.members("APPROVED"))
        self.assertNotIn("held", self.members("IN_REVIEW"))

    def test_creators_see_approval_activity_only_at_handoff(self):
        # SQLite uses numeric JSON booleans; a string matches PostgreSQL ->> text output.
        expression = ACTIVITY_ACTIONS.replace("(e.data->>'version')::int", "CAST(e.data->>'version' AS INT)")
        for team, event, expected in (
            (False, "REVIEW_DECISION", None),
            (False, "REVIEW_HANDOFF_RELEASED", "approved"),
            (True, "REVIEW_DECISION", "approved"),
            (True, "REVIEW_HANDOFF_RELEASED", None),
        ):
            with self.subTest(team=team, event=event):
                sql = expression.replace("$2", "1" if team else "0")
                actual = self.conn.execute(
                    f"SELECT {sql} FROM (SELECT ? AS event_type, ? AS data) e",
                    (event, '{"decision":"APPROVE","handoff_pending":"true"}'),
                ).fetchone()[0]
                self.assertEqual(actual, expected)
