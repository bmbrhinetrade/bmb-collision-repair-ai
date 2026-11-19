"""Dedupe by post id: Store LRU fast path, SQLite primary key across restarts, and the pipeline on top.

Also the Store's alert bookkeeping: schema migration of older databases, the latest alert per post
(edit re-alerts) and the stats used by /health and the daily summary.
"""

from __future__ import annotations

import asyncio
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from ceoscan.alerts.dispatcher import AlertDispatcher
from ceoscan.alerts.notifiers import CaptureNotifier
from ceoscan.config import AppConfig, Secrets, Settings
from ceoscan.models import (
    Account,
    AlertMessage,
    AlertRecord,
    ChannelResult,
    IngestItem,
    IngestStatus,
    Post,
    RuleResult,
    Score,
    Watchlist,
)
from ceoscan.pipeline import Pipeline
from ceoscan.scoring.claude import OfflineScorer
from ceoscan.store import Store
from ceoscan.timeutil import datetime_to_snowflake

NOON_PT = datetime(2026, 11, 19, 20, 0, tzinfo=UTC)  # 12:00 PST: outside quiet hours
ELON = Account(handle="elonmusk", tickers=("TSLA", "SPCX"), user_id="44196397")
WATCHLIST = Watchlist(
    accounts=(ELON,),
    keyword_rules=(),
    my_tickers=("TSLA", "SPCX"),
    company_names={"TSLA": ("Tesla",)},
)
HIGH_TEXT = "Tesla Q3: record 512,000 deliveries and we are raising full-year guidance."  # fast-rule score 9


def make_post(post_id: str, text: str = "hello") -> Post:
    now = datetime(2026, 11, 19, tzinfo=UTC)
    return Post(id=post_id, text=text, author_id="1", created_at=now, received_at=now, source="stream")


def post_count(store: Store) -> int:
    return int(store._db.execute("SELECT COUNT(*) FROM posts").fetchone()[0])


def alert_count(store: Store) -> int:
    return int(store._db.execute("SELECT COUNT(*) FROM alerts").fetchone()[0])


def snowflake(offset: int = 0, seconds_before: float = 3.0) -> str:
    return str(datetime_to_snowflake(NOON_PT - timedelta(seconds=seconds_before)) + offset)


def payload(post_id: str, text: str = HIGH_TEXT) -> dict:
    return {
        "data": {"id": post_id, "text": text, "author_id": ELON.user_id, "edit_history_tweet_ids": [post_id]},
        "includes": {"users": [{"id": ELON.user_id, "username": "elonmusk", "name": "Elon Musk"}]},
        "matching_rules": [{"id": "1", "tag": "ceoscan:acct:r1t0:1"}],
    }


def make_pipeline(store: Store) -> tuple[Pipeline, CaptureNotifier]:
    settings = Settings()
    cfg = AppConfig(settings, WATCHLIST, Secrets(), Path("settings.yaml"), Path("watchlist.yaml"))
    capture = CaptureNotifier()
    dispatcher = AlertDispatcher(settings.alerts, push=[capture])
    pipeline = Pipeline(cfg, store, OfflineScorer(settings.scoring), dispatcher, IngestStatus(), clock=lambda: NOON_PT)
    return pipeline, capture


def item(p: dict, source: str = "stream") -> IngestItem:
    return IngestItem(payload=p, received_at=NOON_PT - timedelta(seconds=1), source=source)  # type: ignore[arg-type]


# --------------------------------------------------------------------------- Store


def test_same_id_twice_second_ignored() -> None:
    store = Store(":memory:")
    assert store.insert_post_if_new(make_post("100", "first")) is True
    assert store.insert_post_if_new(make_post("100", "second copy")) is False
    assert post_count(store) == 1
    assert store.get_post_row("100")["text"] == "first"  # the first copy is kept
    store.close()


def test_dedupe_survives_store_reopen(tmp_path: Path) -> None:
    db = tmp_path / "data" / "ceoscan.db"
    store = Store(db)
    assert store.insert_post_if_new(make_post("200"))
    store.close()

    reopened = Store(db)  # fresh process: empty LRU, dedupe must come from SQLite
    assert "200" not in reopened._seen
    assert reopened.post_exists("200")
    assert reopened.insert_post_if_new(make_post("200")) is False
    assert reopened.insert_post_if_new(make_post("201")) is True
    assert post_count(reopened) == 2
    reopened.close()


class _ExplodingDb:
    """Stands in for the sqlite connection: any query fails the test."""

    def execute(self, *args: object, **kwargs: object) -> None:
        raise AssertionError("LRU hit must not query SQLite")


def test_lru_fast_path_does_not_touch_sqlite() -> None:
    store = Store(":memory:")
    assert store.insert_post_if_new(make_post("300"))
    real_db = store._db
    store._db = _ExplodingDb()  # type: ignore[assignment]
    try:
        assert store.insert_post_if_new(make_post("300")) is False
        assert store.post_exists("300")
    finally:
        store._db = real_db
        store.close()


def test_lru_evicted_id_is_still_deduped_by_sqlite() -> None:
    store = Store(":memory:", lru_size=2)
    for pid in ("1", "2", "3"):
        assert store.insert_post_if_new(make_post(pid))
    assert list(store._seen) == ["2", "3"]  # bounded, oldest evicted
    assert store.insert_post_if_new(make_post("1")) is False  # primary key catches it
    assert list(store._seen) == ["3", "1"]  # and it is back in the LRU
    assert post_count(store) == 3
    store.close()


def test_sqlite_primary_key_enforced() -> None:
    store = Store(":memory:")
    store.insert_post_if_new(make_post("400"))
    with pytest.raises(sqlite3.IntegrityError):
        store._db.execute(
            "INSERT INTO posts (id, author_id, text, created_at_ms, received_at_ms, source) VALUES (?,?,?,?,?,?)",
            ("400", "1", "x", 0, 0, "stream"),
        )
    store.close()


# --------------------------------------------------------------------------- Pipeline


async def test_pipeline_ignores_second_copy_of_same_post() -> None:
    store = Store(":memory:")
    pipeline, capture = make_pipeline(store)
    p = payload(snowflake())

    first = await pipeline.process(item(p))
    second = await pipeline.process(item(p))

    assert first.outcome == "alert" and first.alert is not None
    assert second.outcome == "duplicate"
    assert second.alert is None
    assert len(capture.sent) == 1
    assert post_count(store) == 1
    assert alert_count(store) == 1
    store.close()


async def test_pipeline_dedupes_across_sources() -> None:
    """The same post from the stream and from gap recovery / polling alerts once."""
    store = Store(":memory:")
    pipeline, capture = make_pipeline(store)
    p = payload(snowflake())

    assert await pipeline.handle(item(p, "stream")) is not None
    assert await pipeline.handle(item(p, "recovery")) is None
    assert await pipeline.handle(item(p, "poll")) is None
    assert len(capture.sent) == 1
    store.close()


async def test_pipeline_dedupe_survives_restart(tmp_path: Path) -> None:
    db = tmp_path / "ceoscan.db"
    p = payload(snowflake())

    store = Store(db)
    pipeline, _capture = make_pipeline(store)
    assert await pipeline.handle(item(p)) is not None
    store.close()

    store2 = Store(db)
    pipeline2, capture2 = make_pipeline(store2)
    res = await pipeline2.process(item(p, "recovery"))
    assert res.outcome == "duplicate"
    assert capture2.sent == []
    assert post_count(store2) == 1
    assert alert_count(store2) == 1
    store2.close()


async def test_concurrent_workers_alert_once_for_duplicates() -> None:
    store = Store(":memory:")
    pipeline, capture = make_pipeline(store)
    pipeline.idle_poll_seconds = 0.01
    queue: asyncio.Queue[IngestItem] = asyncio.Queue()
    p = payload(snowflake())
    for _ in range(5):
        queue.put_nowait(item(p))
    queue.put_nowait(item(payload(snowflake(offset=1))))

    stop = asyncio.Event()
    runner = asyncio.create_task(pipeline.run(queue, stop, workers=4))
    await asyncio.wait_for(queue.join(), timeout=5)
    stop.set()
    await asyncio.wait_for(runner, timeout=5)

    assert len(capture.sent) == 2  # one per distinct id
    assert post_count(store) == 2
    store.close()


# --------------------------------------------------------------------------- Store: alerts, migration, stats

# The alerts table as created by the first release (no "late" column).
OLD_ALERTS_SCHEMA = """
CREATE TABLE alerts (
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
"""


def alert(post_id: str | None = "1", *, ok: bool = True, late: bool = False, latency: int | None = 4000,
          score: int = 8, direction: str = "bullish", tickers: list[str] | None = None,
          kind: str = "post") -> AlertRecord:
    return AlertRecord(
        post_id=post_id, kind=kind, decision_reason="test",  # type: ignore[arg-type]
        message=AlertMessage(title="t", body="b"),
        results=[ChannelResult("capture", ok, 5)], score=score, direction=direction,
        tickers=tickers if tickers is not None else ["TSLA"], late=late,
        total_latency_ms=latency if ok else None,
    )


def columns(store: Store, table: str) -> list[str]:
    return [r["name"] for r in store._db.execute(f"PRAGMA table_info({table})").fetchall()]


def test_old_database_gets_late_column(tmp_path: Path) -> None:
    db = tmp_path / "old.db"
    con = sqlite3.connect(db)
    con.executescript(OLD_ALERTS_SCHEMA)
    con.execute("INSERT INTO alerts (post_id, kind, created_at_ms, sent_ok, total_latency_ms) "
                "VALUES ('9', 'post', 1, 1, 3000)")
    con.commit()
    con.close()

    store = Store(db)
    assert "late" in columns(store, "alerts")
    assert store._db.execute("SELECT late FROM alerts WHERE post_id='9'").fetchone()[0] == 0  # old rows: live
    store.record_alert(alert("10", late=True))
    assert store._db.execute("SELECT late FROM alerts WHERE post_id='10'").fetchone()[0] == 1
    store.close()

    reopened = Store(db)  # idempotent: no second ALTER
    assert columns(reopened, "alerts").count("late") == 1
    reopened.close()


def test_migration_tolerates_concurrent_add(tmp_path: Path) -> None:
    """Another process may add the column between our check and our ALTER."""
    db = tmp_path / "race.db"
    con = sqlite3.connect(db)
    con.executescript(OLD_ALERTS_SCHEMA)
    con.close()

    class RacingStore(Store):
        def _columns(self, table: str) -> set[str]:
            cols = super()._columns(table)
            if table == "alerts" and "late" not in cols and not getattr(self, "_raced", False):
                self._raced = True
                other = sqlite3.connect(db)
                other.execute("ALTER TABLE alerts ADD COLUMN late INTEGER NOT NULL DEFAULT 0")
                other.commit()
                other.close()
            return cols

    store = RacingStore(db)
    assert "late" in columns(store, "alerts")
    store.close()


def test_last_alert_for_post() -> None:
    store = Store(":memory:")
    assert store.last_alert_for_post("1") is None
    store.record_alert(alert("1", score=7, direction="bullish", tickers=["TSLA"]))
    store.record_alert(alert("1", score=9, direction="bearish", tickers=["TSLA", "AMD"]))
    store.record_alert(alert("1", ok=False, score=10, direction="unclear"))  # failed: never seen by the user
    last = store.last_alert_for_post("1")
    assert last is not None
    assert (last["score"], last["direction"], last["tickers"]) == (9, "bearish", ["TSLA", "AMD"])
    assert store.last_alert_for_post("2") is None
    store.close()


def test_stats_since_counts_and_latency_excludes_late_alerts() -> None:
    store = Store(":memory:")
    since = datetime(2020, 1, 1, tzinfo=UTC)
    store.record_alert(alert("1", latency=2000))
    store.record_alert(alert("2", latency=6000))
    store.record_alert(alert("3", latency=7_200_000, late=True))  # 2 h old catch-up post
    store.record_alert(alert("4", ok=False))
    store.record_alert(alert(None, kind="system", latency=None))
    stats = store.stats_since(since)

    assert stats["alerts"] == 3 and stats["alerts_late"] == 1 and stats["alerts_failed"] == 1
    assert stats["avg_latency_ms"] == 4000 and stats["max_latency_ms"] == 6000
    store.close()


def test_stats_since_only_late_alerts_has_no_latency() -> None:
    store = Store(":memory:")
    store.record_alert(alert("1", latency=9_000_000, late=True))
    stats = store.stats_since(datetime(2020, 1, 1, tzinfo=UTC))
    assert stats["alerts"] == 1 and stats["alerts_late"] == 1
    assert stats["avg_latency_ms"] is None and stats["max_latency_ms"] is None
    store.close()


def test_stats_since_counts_score_and_stream_errors() -> None:
    store = Store(":memory:")
    since = datetime(2020, 1, 1, tzinfo=UTC)
    rules = RuleResult(flagged=True)
    for pid, error in (("1", "timeout after 4s"), ("2", None), ("3", "HTTP 401 authentication_error")):
        store.insert_post_if_new(make_post(pid))
        score = Score(tickers=[], direction="unclear", impact=3, reason="r", is_joke=False,
                      source="rules_fallback" if error else "claude", error=error)
        store.update_post_score(pid, rules, score)
    store.log_event("stream_error", "HTTP 503", level="warning")
    store.log_event("poll_error", "HTTP 429", level="warning")
    store.log_event("stream_error", "credits depleted", level="error")
    store.log_event("gap_recovery", "fetched 3", level="info")
    stats = store.stats_since(since)

    assert stats["score_errors"] == 2
    assert stats["last_score_error"] in ("timeout after 4s", "HTTP 401 authentication_error")
    assert stats["stream_errors"] == 3
    assert stats["errors"] == 1  # level='error' events, as before
    # the keys /health and the summary already read are still there
    for key in ("posts", "flagged", "claude_scored", "alerts", "alerts_failed", "avg_latency_ms", "max_latency_ms"):
        assert key in stats
    assert stats["posts"] == 3 and stats["claude_scored"] == 1
    store.close()


def test_stats_since_respects_since() -> None:
    store = Store(":memory:")
    store.record_alert(alert("1"))
    store.log_event("stream_error", "x")
    stats = store.stats_since(datetime(2999, 1, 1, tzinfo=UTC))
    assert stats["alerts"] == 0 and stats["alerts_late"] == 0 and stats["stream_errors"] == 0
    assert stats["score_errors"] == 0 and stats["last_score_error"] is None
    store.close()
