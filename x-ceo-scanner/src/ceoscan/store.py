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
from collections.abc import Iterable
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
    push_ms          INTEGER,
    late             INTEGER NOT NULL DEFAULT 0
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

-- Claude analyst desk: one row per fire of the Claude Code scheduled task (or per dropped batch).
-- Never holds secrets: no token, no fire URL, no request headers.
CREATE TABLE IF NOT EXISTS desk_fires (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    fired_at_ms INTEGER NOT NULL,
    kind        TEXT NOT NULL,
    post_ids    TEXT,
    tickers     TEXT,
    status      TEXT NOT NULL,
    http_status INTEGER,
    session_id  TEXT,
    session_url TEXT,
    error       TEXT,
    attempts    INTEGER NOT NULL DEFAULT 0,
    text_chars  INTEGER
);
CREATE INDEX IF NOT EXISTS idx_desk_fires_at ON desk_fires(fired_at_ms);
"""

#: desk_fires.status values
DESK_SENT, DESK_FAILED, DESK_DROPPED = "sent", "failed", "dropped"

#: Columns added after the first release. ``CREATE TABLE IF NOT EXISTS`` does not touch an existing
#: table, so databases created by an older version get these with ``ALTER TABLE ... ADD COLUMN`` on open.
#: Only additive, constant-default columns belong here (safe for SQLite and for older code reading the DB).
MIGRATIONS: tuple[tuple[str, str, str], ...] = (
    # (table, column, column definition)
    ("alerts", "late", "INTEGER NOT NULL DEFAULT 0"),  # post older than alerts.max_post_age_seconds
)

#: events.kind values written by the X ingest (stream / poller) for connection and API problems
INGEST_ERROR_KINDS: tuple[str, ...] = ("stream_error", "poll_error")

#: posts.source of posts fetched by gap / restart catch-up: their alerts measure the catch-up, not the
#: live post -> phone path, so they are left out of the latency statistics
CATCH_UP_SOURCE = "recovery"


def _like_escape(text: str) -> str:
    """``text`` for a LIKE pattern with ESCAPE '\\': %, _ and the escape character match literally."""
    return text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


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
            self._migrate()

    def _columns(self, table: str) -> set[str]:
        return {str(r["name"]) for r in self._db.execute(f"PRAGMA table_info({table})").fetchall()}

    def _migrate(self) -> None:
        """Add columns missing from a database created by an older version (idempotent)."""
        for table, column, ddl in MIGRATIONS:
            if column in self._columns(table):
                continue
            try:
                self._db.execute(f"ALTER TABLE {table} ADD COLUMN {column} {ddl}")
            except sqlite3.OperationalError:
                # another process (e.g. a CLI command on the same DB) may have added it meanwhile
                if column not in self._columns(table):
                    raise

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
                   scoring_ms, push_ms, late) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
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
                    int(bool(rec.late)),
                ),
            )
            return int(cur.lastrowid or 0)

    def alert_exists_for_post(self, post_id: str) -> bool:
        with self._lock:
            row = self._db.execute(
                "SELECT 1 FROM alerts WHERE post_id=? AND kind='post' AND sent_ok=1", (post_id,)
            ).fetchone()
        return row is not None

    def last_alert_for_post(self, post_id: str) -> dict[str, Any] | None:
        """The newest delivered post alert for ``post_id``: score, direction, tickers (list), created_at_ms."""
        with self._lock:
            row = self._db.execute(
                """SELECT score, direction, tickers, created_at_ms, late FROM alerts
                   WHERE post_id=? AND kind='post' AND sent_ok=1 ORDER BY id DESC LIMIT 1""",
                (post_id,),
            ).fetchone()
        if row is None:
            return None
        out = dict(row)
        try:
            tickers = json.loads(out.get("tickers") or "[]")
        except ValueError:
            tickers = []
        out["tickers"] = [str(t) for t in tickers] if isinstance(tickers, list) else []
        return out

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

    # ------------------------------------------------------------------ Claude analyst desk

    def record_desk_fire(
        self,
        kind: str,
        post_ids: list[str],
        tickers: list[str],
        status: str,
        *,
        http_status: int | None = None,
        session_id: str | None = None,
        session_url: str | None = None,
        error: str | None = None,
        attempts: int = 0,
        text_chars: int | None = None,
        fired_at: datetime | None = None,
    ) -> int:
        with self._lock:
            cur = self._db.execute(
                """INSERT INTO desk_fires (fired_at_ms, kind, post_ids, tickers, status, http_status, session_id,
                   session_url, error, attempts, text_chars) VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    to_ms(fired_at or now_utc()),
                    kind,
                    json.dumps(list(post_ids)),
                    json.dumps(list(tickers)),
                    status,
                    http_status,
                    session_id,
                    session_url,
                    (error or None) and str(error)[:500],
                    int(attempts),
                    text_chars,
                ),
            )
            return int(cur.lastrowid or 0)

    def desk_fires(self, since: datetime | None = None, limit: int = 100) -> list[dict[str, Any]]:
        """Desk fires (newest first), post_ids / tickers decoded."""
        q = "SELECT * FROM desk_fires"
        args: list[Any] = []
        if since is not None:
            q += " WHERE fired_at_ms >= ?"
            args.append(to_ms(since))
        q += " ORDER BY id DESC LIMIT ?"
        args.append(int(limit))
        with self._lock:
            rows = [dict(r) for r in self._db.execute(q, args).fetchall()]
        for row in rows:
            for key in ("post_ids", "tickers"):
                try:
                    value = json.loads(row.get(key) or "[]")
                except ValueError:
                    value = []
                row[key] = [str(v) for v in value] if isinstance(value, list) else []
        return rows

    def desk_sent_times_ms(self, since: datetime) -> list[int]:
        """fired_at_ms of every successful desk fire since ``since`` (oldest first): the hourly budget."""
        with self._lock:
            rows = self._db.execute(
                "SELECT fired_at_ms FROM desk_fires WHERE fired_at_ms >= ? AND status = ? ORDER BY fired_at_ms",
                (to_ms(since), DESK_SENT),
            ).fetchall()
        return [int(r[0]) for r in rows]

    def post_alerts_since(
        self,
        since: datetime,
        *,
        min_score: int | None = None,
        limit: int = 500,
        newest_first: bool = False,
        tickers: Iterable[str] | None = None,
        author_ids: Iterable[str] | None = None,
        exclude_post_ids: Iterable[str] | None = None,
    ) -> list[dict[str, Any]]:
        """Delivered post alerts since ``since`` with the post's author, reason and text.

        Oldest first, or newest first with ``newest_first`` (``limit`` applies in that order, so
        ``newest_first=True`` returns the newest ``limit`` alerts). With ``tickers`` and/or ``author_ids``
        only alerts that name one of the tickers or come from one of the authors are returned.

        Keys: post_id, created_at_ms (alert), score, direction, tickers (list), late, author_id,
        author_username, reason, text, post_created_at_ms. Used by the Claude desk for context and the
        daily review.
        """
        q = """SELECT a.post_id, a.created_at_ms, a.score, a.direction, a.tickers, a.late, p.author_id,
                      p.author_username, p.reason, p.text, p.created_at_ms AS post_created_at_ms
               FROM alerts a LEFT JOIN posts p ON p.id = a.post_id
               WHERE a.kind = 'post' AND a.sent_ok = 1 AND a.created_at_ms >= ?"""
        args: list[Any] = [to_ms(since)]
        if min_score is not None:
            q += " AND a.score >= ?"
            args.append(int(min_score))
        if tickers is not None or author_ids is not None:
            match: list[str] = []
            for t in dict.fromkeys(str(t) for t in tickers or ()):
                match.append("a.tickers LIKE ? ESCAPE '\\'")  # alerts.tickers is a JSON list: ["TSLA", ...]
                args.append(f"%{_like_escape(json.dumps(t))}%")
            ids = list(dict.fromkeys(str(i) for i in author_ids or ()))
            if ids:
                match.append(f"p.author_id IN ({','.join('?' for _ in ids)})")
                args.extend(ids)
            q += f" AND ({' OR '.join(match)})" if match else " AND 0"
        excluded = list(dict.fromkeys(str(i) for i in exclude_post_ids or ()))
        if excluded:
            q += f" AND a.post_id NOT IN ({','.join('?' for _ in excluded)})"
            args.extend(excluded)
        order = " DESC" if newest_first else ""
        q += f" ORDER BY a.created_at_ms{order}, a.id{order} LIMIT ?"
        args.append(int(limit))
        with self._lock:
            rows = [dict(r) for r in self._db.execute(q, args).fetchall()]
        for row in rows:
            try:
                tickers = json.loads(row.get("tickers") or "[]")
            except ValueError:
                tickers = []
            row["tickers"] = [str(t) for t in tickers] if isinstance(tickers, list) else []
        return rows

    # ------------------------------------------------------------------ stats

    def stats_since(self, since: datetime) -> dict[str, Any]:
        """Counts for /health and the daily summary. `since` is usually local midnight.

        * ``alerts`` counts every delivered post alert; ``alerts_late`` how many of those were late
          (catch-up after an outage). Late alerts, and alerts for posts fetched by gap / restart catch-up
          (source ``recovery``), are left out of ``avg_latency_ms`` / ``max_latency_ms``, which measure the
          live post -> phone path only.
        * ``score_errors``: posts whose Claude scoring failed (rules-only fallback used), with the newest
          error text in ``last_score_error``.
        * ``stream_errors``: X ingest problems (``events.kind`` in :data:`INGEST_ERROR_KINDS`, any level).
        * ``errors``: events with level 'error'.
        * ``desk_fires``: Claude analyst desk runs started (fires answered 200); ``desk_errors``: fires that
          failed or alerts that were dropped before a fire.
        """
        ms = to_ms(since)
        kinds = ",".join("?" for _ in INGEST_ERROR_KINDS)
        with self._lock:
            posts = self._db.execute("SELECT COUNT(*) FROM posts WHERE received_at_ms >= ?", (ms,)).fetchone()[0]
            flagged = self._db.execute(
                "SELECT COUNT(*) FROM posts WHERE received_at_ms >= ? AND flagged=1", (ms,)
            ).fetchone()[0]
            scored = self._db.execute(
                "SELECT COUNT(*) FROM posts WHERE received_at_ms >= ? AND score_source='claude'", (ms,)
            ).fetchone()[0]
            sent, late = self._db.execute(
                """SELECT COUNT(*), COALESCE(SUM(late), 0) FROM alerts
                   WHERE created_at_ms >= ? AND kind='post' AND sent_ok=1""",
                (ms,),
            ).fetchone()
            latency = self._db.execute(
                """SELECT AVG(total_latency_ms), MAX(total_latency_ms) FROM alerts
                   WHERE created_at_ms >= ? AND kind='post' AND sent_ok=1 AND late=0
                     AND NOT EXISTS (SELECT 1 FROM posts p WHERE p.id = alerts.post_id AND p.source = ?)""",
                (ms, CATCH_UP_SOURCE),
            ).fetchone()
            failed = self._db.execute(
                "SELECT COUNT(*) FROM alerts WHERE created_at_ms >= ? AND kind='post' AND sent_ok=0", (ms,)
            ).fetchone()[0]
            errors = self._db.execute(
                "SELECT COUNT(*) FROM events WHERE ts_ms >= ? AND level='error'", (ms,)
            ).fetchone()[0]
            score_errors = self._db.execute(
                "SELECT COUNT(*) FROM posts WHERE received_at_ms >= ? AND score_error IS NOT NULL", (ms,)
            ).fetchone()[0]
            last_score_error = self._db.execute(
                """SELECT score_error FROM posts WHERE received_at_ms >= ? AND score_error IS NOT NULL
                   ORDER BY scored_at_ms DESC, received_at_ms DESC LIMIT 1""",
                (ms,),
            ).fetchone()
            stream_errors = self._db.execute(
                f"SELECT COUNT(*) FROM events WHERE ts_ms >= ? AND kind IN ({kinds})",
                (ms, *INGEST_ERROR_KINDS),
            ).fetchone()[0]
            desk_sent, desk_errors = self._db.execute(
                """SELECT COALESCE(SUM(status = ?), 0), COALESCE(SUM(status != ?), 0) FROM desk_fires
                   WHERE fired_at_ms >= ?""",
                (DESK_SENT, DESK_SENT, ms),
            ).fetchone()
        return {
            "posts": posts,
            "flagged": flagged,
            "claude_scored": scored,
            "alerts": sent,
            "alerts_late": int(late),
            "alerts_failed": failed,
            "avg_latency_ms": int(latency[0]) if latency[0] is not None else None,
            "max_latency_ms": int(latency[1]) if latency[1] is not None else None,
            "errors": errors,
            "score_errors": score_errors,
            "last_score_error": last_score_error[0] if last_score_error else None,
            "stream_errors": stream_errors,
            "desk_fires": int(desk_sent),
            "desk_errors": int(desk_errors),
        }
