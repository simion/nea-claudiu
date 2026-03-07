from __future__ import annotations

import sqlite3
import threading
from pathlib import Path


class StateDB:
    def __init__(self, db_path: str):
        path = Path(db_path).expanduser()
        path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(path), check_same_thread=False)
        self.conn.execute('PRAGMA journal_mode=WAL')
        self.conn.row_factory = sqlite3.Row
        self._lock = threading.Lock()
        self._init_schema()

    def _init_schema(self):
        self.conn.executescript(
            """
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

            CREATE INDEX IF NOT EXISTS idx_reviews_repo_pr
                ON reviews (repo_slug, pr_id);

            CREATE INDEX IF NOT EXISTS idx_posted_comments_repo_pr
                ON posted_comments (repo_slug, pr_id);
        """
        )

    def has_review(self, repo_slug: str, pr_id: int, source_commit: str) -> bool:
        with self._lock:
            row = self.conn.execute(
                'SELECT 1 FROM reviews WHERE repo_slug = ? AND pr_id = ? AND source_commit = ? AND status IN (?, ?)',
                (repo_slug, pr_id, source_commit, 'success', 'in_progress'),
            ).fetchone()
            return row is not None

    def start_review(self, repo_slug: str, pr_id: int, source_commit: str):
        with self._lock:
            self.conn.execute(
                'INSERT OR REPLACE INTO reviews (repo_slug, pr_id, source_commit, status) VALUES (?, ?, ?, ?)',
                (repo_slug, pr_id, source_commit, 'in_progress'),
            )
            self.conn.commit()

    def finish_review(self, repo_slug: str, pr_id: int, source_commit: str, *, error: str | None = None):
        with self._lock:
            status = 'error' if error else 'success'
            self.conn.execute(
                'UPDATE reviews SET status = ?, finished_at = CURRENT_TIMESTAMP, error_message = ? '
                'WHERE repo_slug = ? AND pr_id = ? AND source_commit = ?',
                (status, error, repo_slug, pr_id, source_commit),
            )
            self.conn.commit()

    def record_comment(self, repo_slug: str, pr_id: int, comment_id: int):
        with self._lock:
            self.conn.execute(
                'INSERT INTO posted_comments (repo_slug, pr_id, comment_id) VALUES (?, ?, ?)',
                (repo_slug, pr_id, comment_id),
            )
            self.conn.commit()

    def get_comment_ids(self, repo_slug: str, pr_id: int) -> list[int]:
        with self._lock:
            rows = self.conn.execute(
                'SELECT comment_id FROM posted_comments WHERE repo_slug = ? AND pr_id = ?',
                (repo_slug, pr_id),
            ).fetchall()
            return [row['comment_id'] for row in rows]

    def delete_comments(self, repo_slug: str, pr_id: int):
        with self._lock:
            self.conn.execute(
                'DELETE FROM posted_comments WHERE repo_slug = ? AND pr_id = ?',
                (repo_slug, pr_id),
            )
            self.conn.commit()

    def has_any_review(self, repo_slug: str, pr_id: int) -> bool:
        with self._lock:
            row = self.conn.execute(
                'SELECT 1 FROM reviews WHERE repo_slug = ? AND pr_id = ? AND status = ?',
                (repo_slug, pr_id, 'success'),
            ).fetchone()
            return row is not None

    def minutes_since_last_review(self, repo_slug: str, pr_id: int) -> float | None:
        with self._lock:
            row = self.conn.execute(
                "SELECT (julianday('now') - julianday(finished_at)) * 1440 as minutes "
                'FROM reviews WHERE repo_slug = ? AND pr_id = ? AND status = ? '
                'ORDER BY finished_at DESC LIMIT 1',
                (repo_slug, pr_id, 'success'),
            ).fetchone()
            if row is None or row['minutes'] is None:
                return None
            return row['minutes']

    def get_review_history(self, repo_slug: str, limit: int = 20) -> list[dict]:
        with self._lock:
            rows = self.conn.execute(
                'SELECT repo_slug, pr_id, source_commit, status, created_at, finished_at, error_message '
                'FROM reviews WHERE repo_slug = ? ORDER BY created_at DESC LIMIT ?',
                (repo_slug, limit),
            ).fetchall()
            return [dict(row) for row in rows]

    def close(self):
        with self._lock:
            self.conn.close()
