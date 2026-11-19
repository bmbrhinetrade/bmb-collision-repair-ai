"""Dedupe by post id: Store LRU fast path, SQLite primary key across restarts, and the pipeline on top."""

from __future__ import annotations

import asyncio
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from ceoscan.alerts.dispatcher import AlertDispatcher
from ceoscan.alerts.notifiers import CaptureNotifier
from ceoscan.config import AppConfig, Secrets, Settings
from ceoscan.models import Account, IngestItem, IngestStatus, Post, Watchlist
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
