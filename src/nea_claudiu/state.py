from __future__ import annotations

import sqlite3
from pathlib import Path


class StateDB:
    def __init__(self, db_path: str):
        path = Path(db_path).expanduser()
        path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(path))
        self.conn.row_factory = sqlite3.Row
        self._init_schema()

    def _init_schema(self):
        self.conn.executescript('''
            CREATE TABLE IF NOT EXISTS reviews (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                repo_slug TEXT NOT NULL,
                pr_id INTEGER NOT NULL,
                source_commit TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'pending',
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                finished_at TIMESTAMP,
                error_message TEXT,
                UNIQUE(repo_slug, pr_id, source_commit)
            );

            CREATE TABLE IF NOT EXISTS posted_comments (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                repo_slug TEXT NOT NULL,
                pr_id INTEGER NOT NULL,
                comment_id INTEGER NOT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
        ''')

    def has_review(self, repo_slug: str, pr_id: int, source_commit: str) -> bool:
        row = self.conn.execute(
            'SELECT 1 FROM reviews WHERE repo_slug = ? AND pr_id = ? AND source_commit = ? AND status IN (?, ?)',
            (repo_slug, pr_id, source_commit, 'success', 'in_progress'),
        ).fetchone()
        return row is not None

    def start_review(self, repo_slug: str, pr_id: int, source_commit: str):
        self.conn.execute(
            'INSERT OR REPLACE INTO reviews (repo_slug, pr_id, source_commit, status) VALUES (?, ?, ?, ?)',
            (repo_slug, pr_id, source_commit, 'in_progress'),
        )
        self.conn.commit()

    def finish_review(self, repo_slug: str, pr_id: int, source_commit: str, *, error: str | None = None):
        status = 'error' if error else 'success'
        self.conn.execute(
            'UPDATE reviews SET status = ?, finished_at = CURRENT_TIMESTAMP, error_message = ? '
            'WHERE repo_slug = ? AND pr_id = ? AND source_commit = ?',
            (status, error, repo_slug, pr_id, source_commit),
        )
        self.conn.commit()

    def record_comment(self, repo_slug: str, pr_id: int, comment_id: int):
        self.conn.execute(
            'INSERT INTO posted_comments (repo_slug, pr_id, comment_id) VALUES (?, ?, ?)',
            (repo_slug, pr_id, comment_id),
        )
        self.conn.commit()

    def get_review_history(self, repo_slug: str, limit: int = 20) -> list[dict]:
        rows = self.conn.execute(
            'SELECT repo_slug, pr_id, source_commit, status, created_at, finished_at, error_message '
            'FROM reviews WHERE repo_slug = ? ORDER BY created_at DESC LIMIT ?',
            (repo_slug, limit),
        ).fetchall()
        return [dict(row) for row in rows]

    def close(self):
        self.conn.close()
