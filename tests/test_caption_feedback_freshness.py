"""Old feedback remains auditable but cannot clear revised caption findings."""
import sqlite3
import pytest
from services.compliance.submissions.caption_review_service import feedback_is_current_sql


@pytest.mark.parametrize('alias,timestamp', [('f', 'decided_at'), ('v', 'decided_at'), ('r', 'requested_at')])
def test_feedback_must_postdate_the_caption_edit_for_its_version(alias, timestamp):
    # Exercise the actual correlated predicate against real rows; the JSON operator and CAST are shared SQL.
    with sqlite3.connect(':memory:') as db:
        db.execute('CREATE TABLE content_submission_events (submission_id TEXT,event_type TEXT,data TEXT,created_at TEXT)')
        db.execute(f'CREATE TABLE feedback (id INTEGER,submission_id TEXT,version_number INTEGER,{timestamp} TEXT)')
        db.execute("INSERT INTO content_submission_events VALUES ('post','CAPTION_EDITED','{\"version\":2}','2026-11-24T12:00:00')")
        db.executemany(f'INSERT INTO feedback VALUES (?,?,?,?)', [
            (1, 'post', 2, '2026-11-24T11:00:00'),
            (2, 'post', 2, '2026-11-24T13:00:00'),
            (3, 'other-post', 2, '2026-11-24T11:00:00'),
            (4, 'post', 1, '2026-11-24T11:00:00'),
        ])
        rows = db.execute(f'SELECT id FROM feedback {alias} WHERE ' + feedback_is_current_sql(alias, timestamp)).fetchall()
        assert rows == [(2,), (3,), (4,)]
        assert db.execute('SELECT count(*) FROM feedback').fetchone()[0] == 4
