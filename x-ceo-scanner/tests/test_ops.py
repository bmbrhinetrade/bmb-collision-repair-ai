"""Tests for ceoscan.ops.health (HTTP /health) and ceoscan.ops.scheduler (ops alerts, daily summary)."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import aiohttp
import pytest
from aiohttp.test_utils import TestClient, TestServer

from ceoscan import __version__
from ceoscan.config import AppConfig, Secrets, Settings
from ceoscan.models import AlertMessage, AlertRecord, ChannelResult, IngestStatus, Post, RuleResult, Score, Watchlist
from ceoscan.ops.health import HealthServer, assess_health
from ceoscan.ops.scheduler import KV_LAST_DAILY_SUMMARY, OpsScheduler, format_duration
from ceoscan.store import Store
from ceoscan.timeutil import datetime_to_snowflake, now_utc

PT = ZoneInfo("America/Los_Angeles")


# --------------------------------------------------------------------------- fixtures / fakes


class FakeDispatcher:
    """Records every message; returns one successful channel result (or raises if told to)."""

    def __init__(self, ok: bool = True, raise_exc: Exception | None = None) -> None:
        self.sent: list[tuple[AlertMessage, int | None]] = []
        self.ok = ok
        self.raise_exc = raise_exc

    async def send(self, msg: AlertMessage, score: int | None = None) -> list[ChannelResult]:
        self.sent.append((msg, score))
        if self.raise_exc is not None:
            raise self.raise_exc
        return [ChannelResult(channel="capture", ok=self.ok, elapsed_ms=5, error=None if self.ok else "HTTP 500")]

    @property
    def channel_names(self) -> list[str]:
        return ["capture"]


@pytest.fixture
def store() -> Store:
    s = Store(":memory:")
    yield s
    s.close()


@pytest.fixture
def settings() -> Settings:
    return Settings()


@pytest.fixture
def cfg(settings: Settings) -> AppConfig:
    wl = Watchlist(accounts=(), keyword_rules=(), my_tickers=(), company_names={})
    return AppConfig(settings, wl, Secrets(), Path("config/settings.yaml"), Path("config/watchlist.yaml"))


def make_post(post_id: int, received_at: datetime, username: str = "elonmusk") -> Post:
    return Post(
        id=str(post_id),
        text="hello",
        author_id="44196397",
        created_at=received_at - timedelta(seconds=4),
        received_at=received_at,
        source="stream",
        author_username=username,
    )


def add_post(store: Store, received_at: datetime, flagged: bool, claude: bool, n: int = 0) -> Post:
    post = make_post(datetime_to_snowflake(received_at - timedelta(seconds=4)) + n, received_at)
    assert store.insert_post_if_new(post)
    score = Score(["TSLA"], "bullish", 8, "r", False, "claude" if claude else "rules_fallback") if flagged else None
    store.update_post_score(post.id, RuleResult(flagged=flagged), score)
    return post


def add_alert(store: Store, post_id: str, total_ms: int, ok: bool = True) -> None:
    msg = AlertMessage(title="t", body="b")
    store.record_alert(
        AlertRecord(
            post_id=post_id,
            kind="post",
            decision_reason="score 8 >= 7",
            message=msg,
            results=[ChannelResult("capture", ok, 10)],
            score=8,
            total_latency_ms=total_ms,
        )
    )


def alert_rows(store: Store, kind: str) -> list[dict]:
    return [dict(r) for r in store._db.execute("SELECT * FROM alerts WHERE kind=? ORDER BY id", (kind,)).fetchall()]


# --------------------------------------------------------------------------- health: status logic


def test_assess_health_states(settings: Settings) -> None:
    now = datetime(2026, 11, 19, 20, 0, tzinfo=UTC)
    started = now - timedelta(hours=1)
    ok = IngestStatus(
        mode="stream",
        state="connected",
        connected_since=now - timedelta(minutes=5),
        last_heartbeat_at=now - timedelta(seconds=3),
    )
    assert assess_health(ok, settings, now, started) == ("ok", [])

    stale = IngestStatus(mode="stream", state="connected", last_heartbeat_at=now - timedelta(seconds=61))
    assert assess_health(stale, settings, now, started)[0] == "degraded"

    short = IngestStatus(mode="stream", state="backoff", down_since=now - timedelta(seconds=30))
    assert assess_health(short, settings, now, started)[0] == "degraded"
    at_threshold = IngestStatus(mode="stream", state="backoff", down_since=now - timedelta(seconds=120))
    assert assess_health(at_threshold, settings, now, started)[0] == "degraded"
    long = IngestStatus(mode="stream", state="backoff", down_since=now - timedelta(seconds=121))
    level, reasons = assess_health(long, settings, now, started)
    assert level == "down" and "121s" in reasons[0]

    credits = IngestStatus(mode="stream", state="credits_depleted", down_since=now - timedelta(seconds=5))
    assert assess_health(credits, settings, now, started) == (
        "down",
        ["X API credits depleted - add credits at console.x.com"],
    )
    assert assess_health(IngestStatus(state="stopped"), settings, now, started)[0] == "down"
    assert assess_health(IngestStatus(state="down"), settings, now, started)[0] == "down"

    assert assess_health(IngestStatus(state="starting"), settings, now, now - timedelta(seconds=10))[0] == "degraded"
    assert assess_health(IngestStatus(state="starting"), settings, now, now - timedelta(seconds=300))[0] == "down"

    polling = IngestStatus(
        mode="poll", state="polling", poll_interval_seconds=2.0, last_heartbeat_at=now - timedelta(seconds=2)
    )
    assert assess_health(polling, settings, now, started)[0] == "ok"
    # queue almost full
    level, reasons = assess_health(ok, settings, now, started, queue_size=9_500)
    assert level == "degraded" and "queue" in reasons[0]


# --------------------------------------------------------------------------- health: snapshot


def test_snapshot_fields(store: Store, settings: Settings) -> None:
    now = now_utc()
    status = IngestStatus(
        mode="stream",
        state="connected",
        connected_since=now - timedelta(minutes=10),
        last_heartbeat_at=now - timedelta(seconds=2),
        reconnects=3,
        last_error="HTTP 503 Service Unavailable: try later",
    )
    p1 = add_post(store, now - timedelta(seconds=90), flagged=True, claude=True, n=1)
    p2 = add_post(store, now - timedelta(seconds=30), flagged=False, claude=False, n=2)
    add_alert(store, p1.id, total_ms=6000)
    add_alert(store, p1.id, total_ms=9000)
    add_alert(store, p1.id, total_ms=7000, ok=False)
    store.log_event("pipeline_error", "boom", level="error")
    store.log_event("startup", "hi", level="info")
    status.last_post_id, status.last_post_at = p2.id, p2.received_at

    queue: asyncio.Queue = asyncio.Queue()
    queue.put_nowait(object())
    srv = HealthServer("127.0.0.1", 0, status, store, settings, started_at=now - timedelta(hours=2), queue=queue)
    snap = srv.snapshot(now)

    assert set(snap) >= {"status", "mode", "stream", "last_post", "today", "queue_size", "uptime_seconds", "version"}
    assert snap["status"] == "ok"
    assert snap["mode"] == "stream"
    assert snap["version"] == __version__
    assert snap["queue_size"] == 1
    assert snap["uptime_seconds"] == pytest.approx(7200, abs=1)
    s = snap["stream"]
    assert s["state"] == "connected"
    assert s["connected_since"] == status.connected_since.isoformat()
    assert s["last_heartbeat_at"] == status.last_heartbeat_at.isoformat()
    assert s["down_since"] is None and s["down_seconds"] is None
    assert s["reconnects"] == 3
    assert s["last_error"] == "HTTP 503 Service Unavailable: try later"
    lp = snap["last_post"]
    assert lp["id"] == p2.id
    assert lp["handle"] == "elonmusk"
    assert lp["seconds_ago"] == pytest.approx(30, abs=1)
    assert lp["at"] is not None and lp["at_local"].endswith("PT")
    t = snap["today"]
    assert t["posts"] == 2
    assert t["flagged"] == 1
    assert t["alerts"] == 2
    assert t["alerts_failed"] == 1
    assert t["avg_latency_ms"] == 7500
    assert t["max_latency_ms"] == 9000
    assert t["errors"] == 1


def test_snapshot_when_down_and_empty(store: Store, settings: Settings) -> None:
    now = now_utc()
    status = IngestStatus(mode="stream", state="backoff", down_since=now - timedelta(seconds=150), reconnects=1)
    snap = HealthServer("127.0.0.1", 0, status, store, settings, started_at=now).snapshot(now)
    assert snap["status"] == "down"
    assert snap["stream"]["down_seconds"] == pytest.approx(150, abs=1)
    assert snap["last_post"]["id"] is None and snap["last_post"]["seconds_ago"] is None
    assert snap["today"]["posts"] == 0 and snap["today"]["avg_latency_ms"] is None
    assert snap["queue_size"] is None


# --------------------------------------------------------------------------- health: HTTP


async def test_health_http_routes_and_strict(store: Store, settings: Settings) -> None:
    now = now_utc()
    status = IngestStatus(mode="stream", state="connected", connected_since=now, last_heartbeat_at=now)
    srv = HealthServer("127.0.0.1", 0, status, store, settings, started_at=now)
    async with TestClient(TestServer(srv.make_app())) as client:
        resp = await client.get("/health")
        assert resp.status == 200
        assert resp.headers["Content-Type"].startswith("application/json")
        assert resp.headers.get("Cache-Control") == "no-store"
        body = await resp.json()
        assert body["status"] == "ok" and body["stream"]["state"] == "connected"

        resp = await client.get("/")
        assert resp.status == 200 and (await resp.json())["status"] == "ok"

        resp = await client.get("/health?strict=1")
        assert resp.status == 200  # healthy -> 200 even in strict mode

        # stream down for more than 2 minutes
        status.state = "backoff"
        status.connected_since = None
        status.down_since = now_utc() - timedelta(seconds=180)
        resp = await client.get("/health")
        assert resp.status == 200  # non-strict always 200
        assert (await resp.json())["status"] == "down"
        resp = await client.get("/health?strict=1")
        assert resp.status == 503
        assert (await resp.json())["status"] == "down"

        # degraded (down < threshold) is still 200 in strict mode
        status.down_since = now_utc() - timedelta(seconds=10)
        resp = await client.get("/health?strict=1")
        assert resp.status == 200 and (await resp.json())["status"] == "degraded"


async def test_health_server_start_stop(store: Store, settings: Settings) -> None:
    now = now_utc()
    status = IngestStatus(mode="poll", state="polling", last_heartbeat_at=now)
    srv = HealthServer("127.0.0.1", 0, status, store, settings, started_at=now)
    await srv.start()
    try:
        assert srv.port > 0
        async with aiohttp.ClientSession() as session, session.get(f"http://127.0.0.1:{srv.port}/health") as resp:
            assert resp.status == 200
            assert (await resp.json())["mode"] == "poll"
    finally:
        await srv.stop()
    await srv.stop()  # idempotent


# --------------------------------------------------------------------------- scheduler: stream down


def test_format_duration() -> None:
    assert format_duration(45) == "45s"
    assert format_duration(192) == "3m 12s"
    assert format_duration(3725) == "1h 2m 5s"
    assert format_duration(-3) == "0s"


async def test_stream_down_alert_fires_once_then_recovery_once(cfg: AppConfig, store: Store) -> None:
    t0 = datetime(2026, 11, 19, 20, 0, tzinfo=UTC)
    status = IngestStatus(mode="stream", state="backoff", down_since=t0, last_error="HTTP 503 Service Unavailable")
    disp = FakeDispatcher()
    sched = OpsScheduler(cfg, store, status, disp, clock=lambda: t0)  # type: ignore[arg-type]

    assert await sched.check_stream_down(t0 + timedelta(seconds=60)) is None
    assert await sched.check_stream_down(t0 + timedelta(seconds=120)) is None  # needs MORE than 120 s
    rec = await sched.check_stream_down(t0 + timedelta(seconds=121))
    assert rec is not None
    assert rec.kind == "system" and rec.sent_ok
    assert rec.message.title == "X stream down"
    assert rec.message.kind == "system" and rec.message.priority == "high"
    assert "2m 1s" in rec.message.body and "HTTP 503 Service Unavailable" in rec.message.body
    assert len(disp.sent) == 1

    # still down: no repeat alerts
    for s in (130, 300, 3600):
        assert await sched.check_stream_down(t0 + timedelta(seconds=s)) is None
    assert len(disp.sent) == 1

    # reconnecting but not yet connected: still no recovery alert
    status.state = "reconnecting"
    assert await sched.check_stream_down(t0 + timedelta(seconds=3700)) is None

    # healthy again
    status.state = "connected"
    status.down_since = None
    status.connected_since = t0 + timedelta(seconds=250)
    rec2 = await sched.check_stream_down(t0 + timedelta(seconds=255))
    assert rec2 is not None
    assert rec2.message.title == "X stream recovered after 4m 10s"
    assert rec2.message.priority == "normal"
    assert len(disp.sent) == 2
    for s in (260, 400):
        assert await sched.check_stream_down(t0 + timedelta(seconds=s)) is None
    assert len(disp.sent) == 2

    rows = alert_rows(store, "system")
    assert [r["title"] for r in rows] == ["X stream down", "X stream recovered after 4m 10s"]
    events = [e["kind"] for e in store.recent_events(t0 - timedelta(days=3650), limit=50)]
    assert "stream_down_alert" in events and "stream_recovered_alert" in events

    # a new outage alerts again
    status.state, status.down_since, status.connected_since = "backoff", t0 + timedelta(seconds=500), None
    assert await sched.check_stream_down(t0 + timedelta(seconds=600)) is None
    assert (await sched.check_stream_down(t0 + timedelta(seconds=621))) is not None
    assert len(disp.sent) == 3


async def test_no_recovery_alert_without_prior_down_alert(cfg: AppConfig, store: Store) -> None:
    t0 = datetime(2026, 11, 19, 20, 0, tzinfo=UTC)
    status = IngestStatus(mode="stream", state="backoff", down_since=t0)
    disp = FakeDispatcher()
    sched = OpsScheduler(cfg, store, status, disp)  # type: ignore[arg-type]
    assert await sched.check_stream_down(t0 + timedelta(seconds=30)) is None
    status.state, status.down_since, status.connected_since = "connected", None, t0 + timedelta(seconds=40)
    assert await sched.check_stream_down(t0 + timedelta(seconds=45)) is None
    assert disp.sent == []


async def test_credits_depleted_alert(cfg: AppConfig, store: Store) -> None:
    t0 = datetime(2026, 11, 19, 20, 0, tzinfo=UTC)
    status = IngestStatus(
        mode="stream", state="credits_depleted", down_since=t0, last_error="credits depleted: HTTP 402 CreditsDepleted"
    )
    disp = FakeDispatcher()
    sched = OpsScheduler(cfg, store, status, disp)  # type: ignore[arg-type]
    rec = await sched.check_stream_down(t0 + timedelta(seconds=125))
    assert rec is not None
    assert rec.message.title == "X API credits depleted - add credits at console.x.com"
    assert "402" in rec.message.body
    assert await sched.check_stream_down(t0 + timedelta(seconds=500)) is None
    assert len(disp.sent) == 1


async def test_down_state_without_down_since_uses_first_observation(cfg: AppConfig, store: Store) -> None:
    t0 = datetime(2026, 11, 19, 20, 0, tzinfo=UTC)
    status = IngestStatus(mode="poll", state="credits_depleted")  # down_since never set
    disp = FakeDispatcher()
    sched = OpsScheduler(cfg, store, status, disp)  # type: ignore[arg-type]
    assert await sched.check_stream_down(t0) is None
    assert await sched.check_stream_down(t0 + timedelta(seconds=100)) is None
    assert await sched.check_stream_down(t0 + timedelta(seconds=121)) is not None
    assert len(disp.sent) == 1


async def test_poll_mode_titles(cfg: AppConfig, store: Store) -> None:
    t0 = datetime(2026, 11, 19, 20, 0, tzinfo=UTC)
    status = IngestStatus(mode="poll", state="down", down_since=t0)
    disp = FakeDispatcher()
    sched = OpsScheduler(cfg, store, status, disp)  # type: ignore[arg-type]
    rec = await sched.check_stream_down(t0 + timedelta(seconds=200))
    assert rec is not None and rec.message.title == "X timeline polling down"
    status.state, status.down_since = "polling", None
    rec2 = await sched.check_stream_down(t0 + timedelta(seconds=260))
    assert rec2 is not None and rec2.message.title == "X timeline polling recovered after 4m 20s"


async def test_custom_threshold_and_failed_delivery(cfg: AppConfig, store: Store) -> None:
    cfg.settings.ops.stream_down_alert_seconds = 30
    t0 = datetime(2026, 11, 19, 20, 0, tzinfo=UTC)
    status = IngestStatus(mode="stream", state="backoff", down_since=t0)
    disp = FakeDispatcher(raise_exc=RuntimeError("boom"))
    sched = OpsScheduler(cfg, store, status, disp)  # type: ignore[arg-type]
    rec = await sched.check_stream_down(t0 + timedelta(seconds=31))
    assert rec is not None and rec.sent_ok is False  # dispatcher failure is contained
    assert alert_rows(store, "system")[0]["sent_ok"] == 0
    assert await sched.check_stream_down(t0 + timedelta(seconds=60)) is None  # still only once


# --------------------------------------------------------------------------- scheduler: daily summary


async def test_daily_summary_once_per_day_within_window(cfg: AppConfig, store: Store) -> None:
    status = IngestStatus(mode="stream", state="connected")
    disp = FakeDispatcher()
    sched = OpsScheduler(cfg, store, status, disp)  # type: ignore[arg-type]

    def at(day: int, hh: int, mm: int, month: int = 11) -> datetime:
        return datetime(2026, month, day, hh, mm, tzinfo=PT)

    assert await sched.maybe_send_daily_summary(at(19, 17, 59)) is None
    rec = await sched.maybe_send_daily_summary(at(19, 18, 0))
    assert rec is not None
    assert rec.kind == "summary" and rec.message.kind == "summary" and rec.message.priority == "low"
    assert rec.message.title == "ceoscan daily summary - Nov 19"
    assert store.kv_get(KV_LAST_DAILY_SUMMARY) == "2026-11-19"
    assert await sched.maybe_send_daily_summary(at(19, 18, 5)) is None
    assert await sched.maybe_send_daily_summary(at(19, 18, 59)) is None
    assert len(disp.sent) == 1

    # next day: outside the 60-minute window -> skipped, inside -> sent
    assert await sched.maybe_send_daily_summary(at(20, 19, 0)) is None
    assert await sched.maybe_send_daily_summary(at(20, 23, 0)) is None
    assert await sched.maybe_send_daily_summary(at(21, 0, 30)) is None
    # a restarted scheduler (same store) does not resend for a day already sent
    sched2 = OpsScheduler(cfg, store, status, disp)  # type: ignore[arg-type]
    assert await sched2.maybe_send_daily_summary(at(19, 18, 30)) is None
    assert (await sched2.maybe_send_daily_summary(at(21, 18, 59))) is not None
    assert len(disp.sent) == 2
    assert [r["kind"] for r in alert_rows(store, "summary")] == ["summary", "summary"]
    # UTC input works the same: 18:00 PST on Nov 22 == 02:00 UTC Nov 23
    assert (await sched2.maybe_send_daily_summary(datetime(2026, 11, 23, 2, 10, tzinfo=UTC))) is not None
    assert store.kv_get(KV_LAST_DAILY_SUMMARY) == "2026-11-22"


async def test_daily_summary_dst_and_disabled(cfg: AppConfig, store: Store) -> None:
    status = IngestStatus()
    disp = FakeDispatcher()
    sched = OpsScheduler(cfg, store, status, disp)  # type: ignore[arg-type]
    # 2026-07-01 is PDT (UTC-7): 18:00 local == 01:00 UTC next day
    assert await sched.maybe_send_daily_summary(datetime(2026, 7, 2, 0, 59, tzinfo=UTC)) is None
    assert (await sched.maybe_send_daily_summary(datetime(2026, 7, 2, 1, 0, tzinfo=UTC))) is not None
    cfg.settings.ops.daily_summary.enabled = False
    assert await sched.maybe_send_daily_summary(datetime(2026, 7, 3, 1, 0, tzinfo=UTC)) is None
    cfg.settings.ops.daily_summary.enabled = True
    cfg.settings.ops.daily_summary.time = "07:30"
    assert (await sched.maybe_send_daily_summary(datetime(2026, 7, 3, 7, 45, tzinfo=PT))) is not None
    assert len(disp.sent) == 2


def test_build_summary_content(cfg: AppConfig, store: Store) -> None:
    now = now_utc()
    p1 = add_post(store, now - timedelta(seconds=60), flagged=True, claude=True, n=1)
    add_post(store, now - timedelta(seconds=50), flagged=True, claude=False, n=2)
    add_post(store, now - timedelta(seconds=40), flagged=False, claude=False, n=3)
    add_alert(store, p1.id, total_ms=6000)
    add_alert(store, p1.id, total_ms=8400)
    add_alert(store, p1.id, total_ms=1000, ok=False)
    store.log_event("pipeline_error", "a", level="error")
    store.log_event("stream_down_alert", "b", level="error")
    store.log_event("pipeline_error", "c", level="error")
    status = IngestStatus(mode="stream", state="connected", reconnects=4)
    sched = OpsScheduler(cfg, store, status, FakeDispatcher())  # type: ignore[arg-type]

    msg = sched.build_summary(now)
    assert msg.kind == "summary" and msg.priority == "low"
    body = msg.body
    assert "Posts seen: 3 (flagged 2, Claude-scored 1)" in body
    assert "Alerts sent: 2 (1 failed)" in body
    assert "Latency: avg 7.2s, max 8.4s" in body
    assert "Errors: 3 (latest: pipeline_error, stream_down_alert)" in body
    assert "4 reconnect(s)" in body
    assert "Projected X cost at today's volume: $0.45/month (3 posts x $0.005 x 30)" in body
    assert msg.sms_text and "3 posts" in msg.sms_text and len(msg.sms_text) <= 300


def test_build_summary_quiet_day(cfg: AppConfig, store: Store) -> None:
    sched = OpsScheduler(cfg, store, IngestStatus(), FakeDispatcher())  # type: ignore[arg-type]
    body = sched.build_summary(now_utc()).body
    assert "Posts seen: 0" in body
    assert "Latency: n/a (no alerts sent)" in body
    assert "Errors: none" in body
    assert "$0.00/month" in body


# --------------------------------------------------------------------------- scheduler: run loop


async def test_run_loop_alerts_once_and_stops(cfg: AppConfig, store: Store) -> None:
    t0 = datetime(2026, 11, 19, 12, 0, tzinfo=UTC)  # 04:00 PT: no daily summary
    clock_now = [t0 + timedelta(seconds=10)]
    status = IngestStatus(mode="stream", state="backoff", down_since=t0)
    disp = FakeDispatcher()
    sched = OpsScheduler(cfg, store, status, disp, clock=lambda: clock_now[0])  # type: ignore[arg-type]
    stop = asyncio.Event()
    task = asyncio.create_task(sched.run(stop, tick_seconds=0.01))
    await asyncio.sleep(0.05)
    assert disp.sent == []
    clock_now[0] = t0 + timedelta(seconds=200)
    for _ in range(100):
        if disp.sent:
            break
        await asyncio.sleep(0.01)
    await asyncio.sleep(0.05)
    assert len(disp.sent) == 1 and disp.sent[0][0].title == "X stream down"
    stop.set()
    await asyncio.wait_for(task, timeout=1.0)


async def test_run_loop_survives_check_errors(cfg: AppConfig, store: Store, monkeypatch: pytest.MonkeyPatch) -> None:
    status = IngestStatus(mode="stream", state="connected")
    sched = OpsScheduler(cfg, store, status, FakeDispatcher())  # type: ignore[arg-type]
    calls = {"n": 0}

    async def broken(now: datetime) -> None:
        calls["n"] += 1
        raise RuntimeError("kaput")

    monkeypatch.setattr(sched, "check_stream_down", broken)
    stop = asyncio.Event()
    task = asyncio.create_task(sched.run(stop, tick_seconds=0.01))
    await asyncio.sleep(0.06)
    stop.set()
    await asyncio.wait_for(task, timeout=1.0)
    assert calls["n"] >= 2
    errors = store.recent_events(now_utc() - timedelta(minutes=1), level="error")
    assert errors and errors[0]["kind"] == "ops_error"
