"""SQLite persistence: every post, every alert, dedupe, since_ids and ops events.

One connection, used from the event-loop thread only. Writes are tiny and local
(WAL mode), so calling them synchronously from coroutines is fine at this volume
(well under 100 writes/second).
"""

from __future__ import annotations

import json
import sqlite3
import threading
from collections import OrderedDict
from dataclasses import asdict
from datetime import datetime
from pathlib import Path
from typing import Any

from .models import AlertRecord, Post, RuleResult, Score
from .timeutil import now_utc, to_ms

SCHEMA = """
CREATE TABLE IF NOT EXISTS posts (
    id              TEXT PRIMARY KEY,
    author_id       TEXT NOT NULL,
    author_username TEXT,
    text            TEXT NOT NULL,
    created_at_ms   INTEGER NOT NULL,
    received_at_ms  INTEGER NOT NULL,
    source          TEXT NOT NULL,
    matching_rules  TEXT,
    referenced      TEXT,
    raw             TEXT,
    flagged         INTEGER,
    rule_reasons    TEXT,
    score           INTEGER,
    direction       TEXT,
    tickers         TEXT,
    reason          TEXT,
    is_joke         INTEGER,
    score_source    TEXT,
    score_error     TEXT,
    scored_at_ms    INTEGER
);
CREATE INDEX IF NOT EXISTS idx_posts_received ON posts(received_at_ms);
CREATE INDEX IF NOT EXISTS idx_posts_author ON posts(author_id, id);

CREATE TABLE IF NOT EXISTS alerts (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    post_id          TEXT,
    kind             TEXT NOT NULL,
    created_at_ms    INTEGER NOT NULL,
    decision_reason  TEXT,
    score            INTEGER,
    direction        TEXT,
    tickers          TEXT,
    title            TEXT,
    body             TEXT,
    channels         TEXT,
    sent_ok          INTEGER NOT NULL,
    quiet_hours      INTEGER NOT NULL DEFAULT 0,
    total_latency_ms INTEGER,
    x_delay_ms       INTEGER,
    scoring_ms       INTEGER,
    push_ms          INTEGER
);
CREATE INDEX IF NOT EXISTS idx_alerts_created ON alerts(created_at_ms);
CREATE INDEX IF NOT EXISTS idx_alerts_post ON alerts(post_id);

CREATE TABLE IF NOT EXISTS accounts (
    handle        TEXT PRIMARY KEY,
    user_id       TEXT,
    name          TEXT,
    verified      INTEGER,
    verified_type TEXT,
    followers     INTEGER,
    status        TEXT,
    problems      TEXT,
    checked_at_ms INTEGER
);

CREATE TABLE IF NOT EXISTS since_ids (
    user_id  TEXT PRIMARY KEY,
    since_id TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS events (
    id     INTEGER PRIMARY KEY AUTOINCREMENT,
    ts_ms  INTEGER NOT NULL,
    level  TEXT NOT NULL,
    kind   TEXT NOT NULL,
    detail TEXT
);
CREATE INDEX IF NOT EXISTS idx_events_ts ON events(ts_ms);

CREATE TABLE IF NOT EXISTS kv (
    key   TEXT PRIMARY KEY,
    value TEXT
);
"""


class Store:
    """Thin synchronous wrapper around sqlite3.

    Dedupe has two layers: an in-memory LRU of recent ids (fast path) and the
    posts table primary key (survives restarts).
    """

    def __init__(self, path: str | Path, lru_size: int = 50_000) -> None:
        self.path = str(path)
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(self.path, check_same_thread=False, isolation_level=None)
        self._db.row_factory = sqlite3.Row
        self._lock = threading.Lock()
        self._seen: OrderedDict[str, None] = OrderedDict()
        self._lru_size = lru_size
        with self._lock:
            if self.path != ":memory:":
                self._db.execute("PRAGMA journal_mode=WAL")
            self._db.execute("PRAGMA synchronous=NORMAL")
            self._db.executescript(SCHEMA)

    def close(self) -> None:
        with self._lock:
            self._db.close()

    # ------------------------------------------------------------------ posts / dedupe

    def _remember(self, post_id: str) -> None:
        self._seen[post_id] = None
        self._seen.move_to_end(post_id)
        while len(self._seen) > self._lru_size:
            self._seen.popitem(last=False)

    def post_exists(self, post_id: str) -> bool:
        if post_id in self._seen:
            return True
        with self._lock:
            row = self._db.execute("SELECT 1 FROM posts WHERE id=?", (post_id,)).fetchone()
        if row:
            self._remember(post_id)
        return row is not None

    def insert_post_if_new(self, post: Post) -> bool:
        """Insert the post; return False if we have seen this post id before (dedupe)."""
        if post.id in self._seen:
            return False
        with self._lock:
            cur = self._db.execute(
                """INSERT OR IGNORE INTO posts
                   (id, author_id, author_username, text, created_at_ms, received_at_ms, source,
                    matching_rules, referenced, raw)
                   VALUES (?,?,?,?,?,?,?,?,?,?)""",
                (
                    post.id,
                    post.author_id,
                    post.author_username,
                    post.text,
                    to_ms(post.created_at),
                    to_ms(post.received_at),
                    post.source,
                    json.dumps(post.matching_rule_tags),
                    json.dumps([asdict(r) for r in post.referenced]),
                    json.dumps(post.raw, separators=(",", ":")),
                ),
            )
        self._remember(post.id)
        return cur.rowcount == 1

    def update_post_score(self, post_id: str, rules: RuleResult, score: Score | None) -> None:
        with self._lock:
            self._db.execute(
                """UPDATE posts SET flagged=?, rule_reasons=?, score=?, direction=?, tickers=?,
                   reason=?, is_joke=?, score_source=?, score_error=?, scored_at_ms=? WHERE id=?""",
                (
                    int(rules.flagged),
                    json.dumps(rules.reasons),
                    score.impact if score else None,
                    score.direction if score else None,
                    json.dumps(score.tickers if score else rules.tickers),
                    score.reason if score else None,
                    int(score.is_joke) if score else None,
                    score.source if score else "skipped",
                    score.error if score else None,
                    to_ms(now_utc()),
                    post_id,
                ),
            )

    def get_post_row(self, post_id: str) -> dict[str, Any] | None:
        with self._lock:
            row = self._db.execute("SELECT * FROM posts WHERE id=?", (post_id,)).fetchone()
        return dict(row) if row else None

    def last_post(self) -> dict[str, Any] | None:
        with self._lock:
            row = self._db.execute(
                "SELECT id, author_id, author_username, received_at_ms, created_at_ms FROM posts "
                "ORDER BY received_at_ms DESC LIMIT 1"
            ).fetchone()
        return dict(row) if row else None

    # ------------------------------------------------------------------ alerts

    def record_alert(self, rec: AlertRecord) -> int:
        with self._lock:
            cur = self._db.execute(
                """INSERT INTO alerts (post_id, kind, created_at_ms, decision_reason, score, direction,
                   tickers, title, body, channels, sent_ok, quiet_hours, total_latency_ms, x_delay_ms,
                   scoring_ms, push_ms) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    rec.post_id,
                    rec.kind,
                    to_ms(now_utc()),
                    rec.decision_reason,
                    rec.score,
                    rec.direction,
                    json.dumps(rec.tickers),
                    rec.message.title,
                    rec.message.body,
                    json.dumps([asdict(r) for r in rec.results]),
                    int(rec.sent_ok),
                    int(rec.quiet_hours),
                    rec.total_latency_ms,
                    rec.x_delay_ms,
                    rec.scoring_ms,
                    rec.push_ms,
                ),
            )
            return int(cur.lastrowid or 0)

    def alert_exists_for_post(self, post_id: str) -> bool:
        with self._lock:
            row = self._db.execute(
                "SELECT 1 FROM alerts WHERE post_id=? AND kind='post' AND sent_ok=1", (post_id,)
            ).fetchone()
        return row is not None

    # ------------------------------------------------------------------ since ids

    def get_since_id(self, user_id: str) -> str | None:
        with self._lock:
            row = self._db.execute("SELECT since_id FROM since_ids WHERE user_id=?", (user_id,)).fetchone()
        return row["since_id"] if row else None

    def set_since_id(self, user_id: str, post_id: str) -> None:
        """Store post_id as the newest seen for user_id, only if it is newer than what we have."""
        with self._lock:
            row = self._db.execute("SELECT since_id FROM since_ids WHERE user_id=?", (user_id,)).fetchone()
            if row and int(row["since_id"]) >= int(post_id):
                return
            self._db.execute(
                "INSERT INTO since_ids (user_id, since_id) VALUES (?, ?) "
                "ON CONFLICT(user_id) DO UPDATE SET since_id=excluded.since_id",
                (user_id, post_id),
            )

    # ------------------------------------------------------------------ accounts

    def upsert_account(
        self,
        handle: str,
        user_id: str | None,
        name: str | None,
        verified: bool | None,
        verified_type: str | None,
        followers: int | None,
        status: str,
        problems: list[str],
    ) -> None:
        with self._lock:
            self._db.execute(
                """INSERT INTO accounts (handle, user_id, name, verified, verified_type, followers, status,
                   problems, checked_at_ms) VALUES (?,?,?,?,?,?,?,?,?)
                   ON CONFLICT(handle) DO UPDATE SET user_id=excluded.user_id, name=excluded.name,
                   verified=excluded.verified, verified_type=excluded.verified_type,
                   followers=excluded.followers, status=excluded.status, problems=excluded.problems,
                   checked_at_ms=excluded.checked_at_ms""",
                (
                    handle.lower(),
                    user_id,
                    name,
                    None if verified is None else int(verified),
                    verified_type,
                    followers,
                    status,
                    json.dumps(problems),
                    to_ms(now_utc()),
                ),
            )

    # ------------------------------------------------------------------ events / kv

    def log_event(self, kind: str, detail: str = "", level: str = "info") -> None:
        with self._lock:
            self._db.execute(
                "INSERT INTO events (ts_ms, level, kind, detail) VALUES (?,?,?,?)",
                (to_ms(now_utc()), level, kind, detail[:2000]),
            )

    def recent_events(self, since: datetime, level: str | None = None, limit: int = 20) -> list[dict[str, Any]]:
        q = "SELECT ts_ms, level, kind, detail FROM events WHERE ts_ms >= ?"
        args: list[Any] = [to_ms(since)]
        if level:
            q += " AND level = ?"
            args.append(level)
        q += " ORDER BY ts_ms DESC LIMIT ?"
        args.append(limit)
        with self._lock:
            return [dict(r) for r in self._db.execute(q, args).fetchall()]

    def kv_get(self, key: str) -> str | None:
        with self._lock:
            row = self._db.execute("SELECT value FROM kv WHERE key=?", (key,)).fetchone()
        return row["value"] if row else None

    def kv_set(self, key: str, value: str) -> None:
        with self._lock:
            self._db.execute(
                "INSERT INTO kv (key, value) VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (key, value),
            )

    # ------------------------------------------------------------------ stats

    def stats_since(self, since: datetime) -> dict[str, Any]:
        """Counts for /health and the daily summary. `since` is usually local midnight."""
        ms = to_ms(since)
        with self._lock:
            posts = self._db.execute("SELECT COUNT(*) FROM posts WHERE received_at_ms >= ?", (ms,)).fetchone()[0]
            flagged = self._db.execute(
                "SELECT COUNT(*) FROM posts WHERE received_at_ms >= ? AND flagged=1", (ms,)
            ).fetchone()[0]
            scored = self._db.execute(
                "SELECT COUNT(*) FROM posts WHERE received_at_ms >= ? AND score_source='claude'", (ms,)
            ).fetchone()[0]
            row = self._db.execute(
                """SELECT COUNT(*), AVG(total_latency_ms), MAX(total_latency_ms) FROM alerts
                   WHERE created_at_ms >= ? AND kind='post' AND sent_ok=1""",
                (ms,),
            ).fetchone()
            failed = self._db.execute(
                "SELECT COUNT(*) FROM alerts WHERE created_at_ms >= ? AND kind='post' AND sent_ok=0", (ms,)
            ).fetchone()[0]
            errors = self._db.execute(
                "SELECT COUNT(*) FROM events WHERE ts_ms >= ? AND level='error'", (ms,)
            ).fetchone()[0]
        return {
            "posts": posts,
            "flagged": flagged,
            "claude_scored": scored,
            "alerts": row[0],
            "alerts_failed": failed,
            "avg_latency_ms": int(row[1]) if row[1] is not None else None,
            "max_latency_ms": int(row[2]) if row[2] is not None else None,
            "errors": errors,
        }
