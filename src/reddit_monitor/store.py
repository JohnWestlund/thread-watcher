"""Local SQLite state: which posts are watched, which comments have been seen,
and what Claude decided about each one (drafts, skips, escalations to the owner)."""

from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path

STATUSES = ("new", "drafted", "needs_owner", "skipped", "replied")

SCHEMA = """
CREATE TABLE IF NOT EXISTS watched_posts (
    post_id      TEXT PRIMARY KEY,
    url          TEXT NOT NULL,
    title        TEXT,
    subreddit    TEXT,
    owner        TEXT NOT NULL,
    added_at     REAL NOT NULL,
    last_checked REAL
);
CREATE TABLE IF NOT EXISTS comments (
    comment_id  TEXT PRIMARY KEY,
    post_id     TEXT NOT NULL,
    author      TEXT,
    body        TEXT,
    permalink   TEXT,
    created_utc REAL,
    first_seen  REAL NOT NULL,
    status      TEXT NOT NULL DEFAULT 'new',
    note        TEXT,
    drafts      TEXT,
    updated_at  REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS comments_by_post ON comments(post_id, status);
CREATE TABLE IF NOT EXISTS authors (
    name          TEXT PRIMARY KEY,
    created_utc   REAL,
    link_karma    INTEGER,
    comment_karma INTEGER,
    suspended     INTEGER,
    fetched_at    REAL NOT NULL
);
"""

AUTHOR_CACHE_SECONDS = 7 * 24 * 3600


class Store:
    def __init__(self, path: str | Path):
        self.path = str(path)
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(self.path, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.executescript(SCHEMA)

    # watched posts

    def watch(self, post_id: str, url: str, title: str, subreddit: str, owner: str) -> None:
        self.db.execute(
            """INSERT INTO watched_posts (post_id, url, title, subreddit, owner, added_at)
               VALUES (?, ?, ?, ?, ?, ?)
               ON CONFLICT(post_id) DO UPDATE SET owner = excluded.owner, title = excluded.title""",
            (post_id, url, title, subreddit, owner, time.time()),
        )
        self.db.commit()

    def unwatch(self, post_id: str) -> bool:
        cur = self.db.execute("DELETE FROM watched_posts WHERE post_id = ?", (post_id,))
        self.db.commit()
        return cur.rowcount > 0

    def watched(self, post_id: str | None = None) -> list[dict]:
        if post_id:
            rows = self.db.execute("SELECT * FROM watched_posts WHERE post_id = ?", (post_id,))
        else:
            rows = self.db.execute("SELECT * FROM watched_posts ORDER BY added_at")
        return [dict(r) for r in rows]

    def mark_checked(self, post_id: str) -> None:
        self.db.execute("UPDATE watched_posts SET last_checked = ? WHERE post_id = ?", (time.time(), post_id))
        self.db.commit()

    # comments

    def is_seen(self, comment_id: str) -> bool:
        return self.db.execute("SELECT 1 FROM comments WHERE comment_id = ?", (comment_id,)).fetchone() is not None

    def record_comment(self, c: dict, post_id: str) -> None:
        now = time.time()
        self.db.execute(
            """INSERT OR IGNORE INTO comments
               (comment_id, post_id, author, body, permalink, created_utc, first_seen, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (c["id"], post_id, c["author"], c["body"], c["permalink"], c["created_utc"], now, now),
        )
        self.db.commit()

    def set_status(self, comment_id: str, status: str, note: str | None = None,
                   drafts: list[str] | None = None) -> bool:
        if status not in STATUSES:
            raise ValueError(f"status must be one of {STATUSES}")
        fields, args = ["status = ?", "updated_at = ?"], [status, time.time()]
        if note is not None:
            fields.append("note = ?")
            args.append(note)
        if drafts is not None:
            fields.append("drafts = ?")
            args.append(json.dumps(drafts))
        cur = self.db.execute(f"UPDATE comments SET {', '.join(fields)} WHERE comment_id = ?", (*args, comment_id))
        self.db.commit()
        return cur.rowcount > 0

    def comments(self, post_id: str | None = None, status: str | None = None,
                 comment_id: str | None = None) -> list[dict]:
        sql, args = "SELECT * FROM comments WHERE 1=1", []
        if comment_id:
            sql += " AND comment_id = ?"
            args.append(comment_id)
        if post_id:
            sql += " AND post_id = ?"
            args.append(post_id)
        if status:
            sql += " AND status = ?"
            args.append(status)
        out = []
        for r in self.db.execute(sql + " ORDER BY created_utc", args):
            d = dict(r)
            d["drafts"] = json.loads(d["drafts"]) if d["drafts"] else []
            out.append(d)
        return out

    # author cache (account age / karma lookups cost an API call each)

    def cached_author(self, name: str) -> dict | None:
        row = self.db.execute("SELECT * FROM authors WHERE name = ?", (name,)).fetchone()
        if row and time.time() - row["fetched_at"] < AUTHOR_CACHE_SECONDS:
            return dict(row)
        return None

    def cache_author(self, info: dict) -> None:
        self.db.execute(
            """INSERT OR REPLACE INTO authors
               (name, created_utc, link_karma, comment_karma, suspended, fetched_at)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (info["name"], info.get("created_utc"), info.get("link_karma"), info.get("comment_karma"),
             int(bool(info.get("suspended"))), time.time()),
        )
        self.db.commit()
