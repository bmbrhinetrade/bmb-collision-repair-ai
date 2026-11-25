"""Tests for ceoscan.ops.health (HTTP /health) and ceoscan.ops.scheduler (ops alerts, daily summary)."""

from __future__ import annotations

import asyncio
import json
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
from ceoscan.ops.scheduler import (
    KV_CONFIG_ERROR_ALERT,
    KV_LAST_DAILY_SUMMARY,
    OpsScheduler,
    format_duration,
    send_config_error_alert,
)
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


def test_config_error_state_is_down_and_503_without_strict(store: Store, settings: Settings) -> None:
    status = IngestStatus(mode="idle", state="config_error", last_error="alerts.timezone: unknown time zone")
    level, reasons = assess_health(status, settings, now_utc(), now_utc())
    assert level == "down"
    assert reasons == [
        "configuration problem: alerts.timezone: unknown time zone - fix the configuration and restart ceoscan"
    ]
    prefixed = IngestStatus(mode="idle", state="config_error", last_error="configuration problem: no accounts")
    assert assess_health(prefixed, settings, now_utc(), now_utc())[1] == [
        "configuration problem: no accounts - fix the configuration and restart ceoscan"
    ]
    srv = HealthServer("127.0.0.1", 0, status, store, settings, now_utc())
    snap = srv.snapshot()
    assert snap["status"] == "down" and snap["state"] == "config_error"
    assert {"alerts_late", "score_errors", "stream_errors"} <= set(snap["today"])


async def test_health_http_config_error_answers_503(store: Store, settings: Settings) -> None:
    status = IngestStatus(mode="idle", state="config_error", last_error="bad")
    srv = HealthServer("127.0.0.1", 0, status, store, settings, now_utc())
    async with TestClient(TestServer(srv.make_app())) as client:
        resp = await client.get("/health")
        assert resp.status == 503  # so the Docker healthcheck marks the container unhealthy
        body = await resp.json()
        assert body["state"] == "config_error" and body["status"] == "down"


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


@pytest.mark.parametrize(
    "last_error",
    [
        "HTTP 401 Unauthorized: Unauthorized",  # the stream connect (what the server reported on Nov 24)
        "stream rule sync failed: HTTP 401 Unauthorized: Unauthorized",  # rule sync at startup
    ],
)
async def test_rejected_token_alert_says_how_to_fix_it(cfg: AppConfig, store: Store, last_error: str) -> None:
    t0 = datetime(2026, 11, 25, 5, 19, 6, tzinfo=UTC)
    status = IngestStatus(mode="stream", state="backoff", down_since=t0, last_error=last_error)
    disp = FakeDispatcher()
    sched = OpsScheduler(cfg, store, status, disp)  # type: ignore[arg-type]
    rec = await sched.check_stream_down(t0 + timedelta(seconds=124))
    assert rec is not None
    assert rec.message.title == "X rejected the API key (HTTP 401) - update X_BEARER_TOKEN"
    body = rec.message.body
    assert "regenerated or revoked at console.x.com" in body
    assert "cd ~/ceoscan && bash x-ceo-scanner/deploy/install-server.sh --dir ~/ceoscan --update-x-token" in body
    assert "retrying will not fix this" in body and "keeps retrying automatically" not in body
    assert "docker compose up -d --force-recreate" in body  # setups without the installer
    assert body.rstrip().endswith(f"Last error: {last_error}")
    assert rec.message.priority == "high"
    assert len(rec.message.sms_text or "") <= 300
    assert await sched.check_stream_down(t0 + timedelta(seconds=600)) is None  # still one alert per outage
    status.state, status.down_since, status.last_error = "connected", None, None
    rec = await sched.check_stream_down(t0 + timedelta(seconds=700))
    assert rec is not None and "recovered" in rec.message.title
    assert len(disp.sent) == 2


@pytest.mark.parametrize(
    "last_error",
    [None, "HTTP 403 Forbidden: no access", "HTTP 503 Service Unavailable", "disconnected by X: HTTP 4010 x"],
)
async def test_other_errors_keep_the_plain_down_alert(cfg: AppConfig, store: Store, last_error: str | None) -> None:
    t0 = datetime(2026, 11, 25, 5, 19, 6, tzinfo=UTC)
    status = IngestStatus(mode="stream", state="backoff", down_since=t0, last_error=last_error)
    disp = FakeDispatcher()
    rec = await OpsScheduler(cfg, store, status, disp).check_stream_down(t0 + timedelta(seconds=124))  # type: ignore[arg-type]
    assert rec is not None and rec.message.title == "X stream down"
    assert "--update-x-token" not in rec.message.body


async def test_credits_depleted_wins_over_a_401_text(cfg: AppConfig, store: Store) -> None:
    t0 = datetime(2026, 11, 25, 5, 19, 6, tzinfo=UTC)
    status = IngestStatus(mode="stream", state="credits_depleted", down_since=t0, last_error="HTTP 401 Unauthorized")
    disp = FakeDispatcher()
    rec = await OpsScheduler(cfg, store, status, disp).check_stream_down(t0 + timedelta(seconds=124))  # type: ignore[arg-type]
    assert rec is not None and rec.message.title.startswith("X API credits depleted")


def test_health_names_a_rejected_token(settings: Settings) -> None:
    now = datetime(2026, 11, 25, 15, 0, tzinfo=UTC)
    started = now - timedelta(hours=12)
    hint = (
        "X rejects the bearer token (HTTP 401): put the current token in .env"
        " (servers set up with install-server.sh: run it with --update-x-token)"
    )
    e401 = "HTTP 401 Unauthorized"
    long = IngestStatus(mode="stream", state="backoff", down_since=now - timedelta(hours=10), last_error=e401)
    level, reasons = assess_health(long, settings, now, started)
    assert level == "down" and reasons[1] == hint
    short = IngestStatus(mode="stream", state="backoff", down_since=now - timedelta(seconds=10), last_error=e401)
    level, reasons = assess_health(short, settings, now, started)
    assert level == "degraded" and hint in reasons
    other = IngestStatus(mode="stream", state="backoff", down_since=now - timedelta(hours=1), last_error="HTTP 503 x")
    assert len(assess_health(other, settings, now, started)[1]) == 1


def test_x_auth_rejected_matches_only_http_401() -> None:
    assert IngestStatus(last_error="HTTP 401 Unauthorized: Unauthorized").x_auth_rejected()
    assert IngestStatus(last_error="stream rule sync failed: HTTP 401 Unauthorized").x_auth_rejected()
    for text in (None, "", "HTTP 403 Forbidden", "HTTP 4010 x", "HTTP 1401 x", "credits depleted: HTTP 402"):
        assert not IngestStatus(last_error=text).x_auth_rejected()


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
    assert await sched.check_stream_down(t0 + timedelta(seconds=60)) is None  # no resend on every tick
    # retried 60 s after the failed attempt; once delivered it is latched
    disp.raise_exc = None
    rec2 = await sched.check_stream_down(t0 + timedelta(seconds=91))
    assert rec2 is not None and rec2.sent_ok and rec2.message.title == "X stream down"
    assert "retry 1" in rec2.decision_reason
    assert await sched.check_stream_down(t0 + timedelta(seconds=2000)) is None
    assert len(disp.sent) == 2


class FlakyDispatcher(FakeDispatcher):
    """Fails the first ``fail_first`` sends, then succeeds."""

    def __init__(self, fail_first: int) -> None:
        super().__init__()
        self.fail_first = fail_first

    async def send(self, msg: AlertMessage, score: int | None = None) -> list[ChannelResult]:
        self.sent.append((msg, score))
        ok = len(self.sent) > self.fail_first
        return [ChannelResult(channel="capture", ok=ok, elapsed_ms=5, error=None if ok else "HTTP 502")]


async def test_down_alert_retried_with_backoff_until_delivered(cfg: AppConfig, store: Store) -> None:
    """REQ-2: a 502 on the single 'down' alert must not silence the whole outage."""
    t0 = datetime(2026, 11, 19, 20, 0, tzinfo=UTC)
    status = IngestStatus(mode="stream", state="backoff", down_since=t0)
    disp = FlakyDispatcher(fail_first=3)
    sched = OpsScheduler(cfg, store, status, disp)  # type: ignore[arg-type]
    attempts_at: list[int] = []
    for sec in range(121, 2000, 5):  # a tick every 5 s for ~31 minutes of outage
        rec = await sched.check_stream_down(t0 + timedelta(seconds=sec))
        if rec is not None:
            attempts_at.append(sec)
    # first try at 121 s, then +60 s, +120 s, +300 s; the 4th attempt is delivered and latched
    assert attempts_at == [121, 181, 301, 601]
    assert [r["sent_ok"] for r in alert_rows(store, "system")] == [0, 0, 0, 1]
    events = store.recent_events(t0 - timedelta(days=1), limit=50)
    failed = [e for e in events if e["kind"] == "stream_down_alert_failed"]
    assert len(failed) == 3

    # recovery alert goes out once; it does not mention a failed down alert (that one got through)
    status.state, status.down_since, status.connected_since = "connected", None, t0 + timedelta(seconds=2000)
    rec = await sched.check_stream_down(t0 + timedelta(seconds=2005))
    assert rec is not None and rec.message.title.startswith("X stream recovered after")
    assert "could not be delivered" not in rec.message.body


async def test_recovery_alert_says_when_down_alert_never_arrived(cfg: AppConfig, store: Store) -> None:
    t0 = datetime(2026, 11, 19, 20, 0, tzinfo=UTC)
    status = IngestStatus(mode="stream", state="backoff", down_since=t0)
    disp = FlakyDispatcher(fail_first=2)  # both down attempts fail, the recovery alert works
    sched = OpsScheduler(cfg, store, status, disp)  # type: ignore[arg-type]
    assert (await sched.check_stream_down(t0 + timedelta(seconds=121))) is not None
    assert (await sched.check_stream_down(t0 + timedelta(seconds=181))) is not None
    status.state, status.down_since, status.connected_since = "connected", None, t0 + timedelta(seconds=200)
    rec = await sched.check_stream_down(t0 + timedelta(seconds=205))
    assert rec is not None and rec.sent_ok
    assert "could not be delivered (2 attempt(s))" in rec.message.body
    assert await sched.check_stream_down(t0 + timedelta(seconds=400)) is None


async def test_failed_recovery_alert_is_retried_a_few_times(cfg: AppConfig, store: Store) -> None:
    t0 = datetime(2026, 11, 19, 20, 0, tzinfo=UTC)
    status = IngestStatus(mode="stream", state="backoff", down_since=t0)
    disp = FakeDispatcher()
    sched = OpsScheduler(cfg, store, status, disp)  # type: ignore[arg-type]
    assert (await sched.check_stream_down(t0 + timedelta(seconds=121))) is not None
    disp.ok = False
    status.state, status.down_since, status.connected_since = "connected", None, t0 + timedelta(seconds=200)
    assert (await sched.check_stream_down(t0 + timedelta(seconds=205))) is not None  # attempt 1 fails
    assert await sched.check_stream_down(t0 + timedelta(seconds=230)) is None  # waits 60 s
    assert (await sched.check_stream_down(t0 + timedelta(seconds=265))) is not None  # attempt 2 fails
    assert (await sched.check_stream_down(t0 + timedelta(seconds=330))) is not None  # attempt 3: give up
    assert await sched.check_stream_down(t0 + timedelta(seconds=500)) is None
    assert len(disp.sent) == 4


async def test_no_channels_does_not_retry_forever(cfg: AppConfig, store: Store) -> None:
    class NoChannels(FakeDispatcher):
        async def send(self, msg: AlertMessage, score: int | None = None) -> list[ChannelResult]:
            self.sent.append((msg, score))
            return []

    t0 = datetime(2026, 11, 19, 20, 0, tzinfo=UTC)
    status = IngestStatus(mode="stream", state="backoff", down_since=t0)
    disp = NoChannels()
    sched = OpsScheduler(cfg, store, status, disp)  # type: ignore[arg-type]
    assert (await sched.check_stream_down(t0 + timedelta(seconds=121))) is not None
    for sec in (200, 500, 1500):
        assert await sched.check_stream_down(t0 + timedelta(seconds=sec)) is None
    assert len(disp.sent) == 1


async def test_config_error_state_sends_no_stream_down_alert(cfg: AppConfig, store: Store) -> None:
    t0 = datetime(2026, 11, 19, 20, 0, tzinfo=UTC)
    status = IngestStatus(mode="idle", state="config_error", down_since=t0, last_error="bad settings")
    disp = FakeDispatcher()
    sched = OpsScheduler(cfg, store, status, disp)  # type: ignore[arg-type]
    assert await sched.check_stream_down(t0 + timedelta(seconds=600)) is None
    assert disp.sent == []


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


async def test_daily_summary_failed_send_is_retried_inside_window(cfg: AppConfig, store: Store) -> None:
    """REQ-2: one failed send at 18:00 must not lose the day's summary."""
    status = IngestStatus(mode="stream", state="connected")
    disp = FlakyDispatcher(fail_first=2)
    sched = OpsScheduler(cfg, store, status, disp)  # type: ignore[arg-type]

    def at(hh: int, mm: int) -> datetime:
        return datetime(2026, 11, 19, hh, mm, tzinfo=PT)

    rec = await sched.maybe_send_daily_summary(at(18, 0))
    assert rec is not None and not rec.sent_ok
    assert store.kv_get(KV_LAST_DAILY_SUMMARY) is None  # not marked as sent
    assert await sched.maybe_send_daily_summary(at(18, 2)) is None  # no resend on every tick
    # a restarted scheduler (same store) honors the 5-minute retry spacing too
    sched2 = OpsScheduler(cfg, store, status, disp)  # type: ignore[arg-type]
    assert await sched2.maybe_send_daily_summary(at(18, 4)) is None
    rec2 = await sched2.maybe_send_daily_summary(at(18, 5))
    assert rec2 is not None and not rec2.sent_ok and "retry 1" in rec2.decision_reason
    rec3 = await sched2.maybe_send_daily_summary(at(18, 10))
    assert rec3 is not None and rec3.sent_ok
    assert store.kv_get(KV_LAST_DAILY_SUMMARY) == "2026-11-19"
    assert await sched2.maybe_send_daily_summary(at(18, 20)) is None
    assert len(disp.sent) == 3
    kinds = [e["kind"] for e in store.recent_events(now_utc() - timedelta(days=1), limit=50)]
    assert kinds.count("daily_summary_failed") == 2 and "daily_summary" in kinds


async def test_daily_summary_retry_stops_when_window_closes(cfg: AppConfig, store: Store) -> None:
    disp = FakeDispatcher(ok=False)
    sched = OpsScheduler(cfg, store, IngestStatus(), disp)  # type: ignore[arg-type]
    for mm in range(0, 60, 5):
        await sched.maybe_send_daily_summary(datetime(2026, 11, 19, 18, mm, tzinfo=PT))
    assert len(disp.sent) == 12
    assert await sched.maybe_send_daily_summary(datetime(2026, 11, 19, 19, 0, tzinfo=PT)) is None
    assert len(disp.sent) == 12


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
    sched = OpsScheduler(
        cfg, store, status, FakeDispatcher(), started_at=now - timedelta(hours=30)  # type: ignore[arg-type]
    )

    msg = sched.build_summary(now)
    assert msg.kind == "summary" and msg.priority == "low"
    body = msg.body
    assert "Posts seen: 3 (flagged 2, Claude-scored 1)" in body
    assert "Alerts sent: 2 (1 failed)" in body
    assert "Latency: avg 7.2s, max 8.4s" in body
    assert "Errors: 3 (latest: pipeline_error, stream_down_alert)" in body
    assert "4 reconnect(s)" in body
    assert "Scoring errors: 0" in body and "Stream errors: 0" in body
    assert "Projected X cost: $0.45/month (3 posts in the last 24 h x $0.005 x 30)" in body
    assert msg.sms_text and "3 posts" in msg.sms_text and "4 reconnects" in msg.sms_text
    assert len(msg.sms_text) <= 300


def test_build_summary_shows_score_and_stream_errors_and_late(cfg: AppConfig, store: Store) -> None:
    class StatsStore:
        """Stands in for the store with the extended stats_since keys."""

        def stats_since(self, since: datetime) -> dict:
            return {
                "posts": 10,
                "flagged": 4,
                "claude_scored": 3,
                "alerts": 3,
                "alerts_late": 1,
                "alerts_failed": 0,
                "avg_latency_ms": 5200,
                "max_latency_ms": 7100,
                "errors": 0,
                "score_errors": 2,
                "last_score_error": "timeout after 4.0s budget",
                "stream_errors": 5,
            }

        def recent_events(self, *a: object, **k: object) -> list:
            return []

    status = IngestStatus(mode="stream", state="connected", reconnects=7)
    now = now_utc()
    sched = OpsScheduler(
        cfg, StatsStore(), status, FakeDispatcher(), started_at=now - timedelta(hours=48)  # type: ignore[arg-type]
    )
    body = sched.build_summary(now).body
    assert "Alerts sent: 3 (0 failed), 1 late (missed during an outage)" in body
    assert "Latency: avg 5.2s, max 7.1s (live alerts; late catch-up alerts excluded)" in body
    assert "Scoring errors: 2 (rules-only score used; latest: timeout after 4.0s budget)" in body
    assert "Stream errors: 5" in body
    assert "7 reconnect(s) since start" in body


def test_build_summary_projection_uses_trailing_24h_of_this_run(cfg: AppConfig, store: Store) -> None:
    """REQ-13: the projection must not use an 18-hour 'today' as if it were a full day."""
    now = now_utc()
    for i in range(12):  # 12 posts in the last 6 hours
        add_post(store, now - timedelta(minutes=30 * i + 1), flagged=False, claude=False, n=i)
    add_post(store, now - timedelta(hours=20), flagged=False, claude=False, n=99)  # before this run started
    sched = OpsScheduler(
        cfg, store, IngestStatus(), FakeDispatcher(), started_at=now - timedelta(hours=6)  # type: ignore[arg-type]
    )
    body = sched.build_summary(now).body
    # 12 posts in 6 h -> 48/day -> 48 x 0.005 x 30 = $7.20
    assert "Projected X cost: $7.20/month (12 posts in 6h 0m 0s since start, scaled to 48/day" in body

    full = OpsScheduler(
        cfg, store, IngestStatus(), FakeDispatcher(), started_at=now - timedelta(days=3)  # type: ignore[arg-type]
    )
    assert "Projected X cost: $1.95/month (13 posts in the last 24 h" in full.build_summary(now).body

    fresh = OpsScheduler(cfg, store, IngestStatus(), FakeDispatcher(), started_at=now)  # type: ignore[arg-type]
    assert "Projected X cost: n/a (running for 0s; needs 1 h of data)" in fresh.build_summary(now).body


def test_build_summary_quiet_day(cfg: AppConfig, store: Store) -> None:
    sched = OpsScheduler(cfg, store, IngestStatus(), FakeDispatcher())  # type: ignore[arg-type]
    body = sched.build_summary(now_utc()).body
    assert "Posts seen: 0" in body
    assert "Latency: n/a (no alerts sent)" in body
    assert "Errors: none" in body
    assert "Projected X cost: n/a" in body


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


# --------------------------------------------------------------------------- configuration-problem alert


async def test_config_error_alert_sent_once_per_problem(store: Store) -> None:
    t0 = datetime(2026, 11, 19, 20, 0, tzinfo=UTC)
    disp = FakeDispatcher()
    problem = "alerts.timezone: unknown time zone 'America/LosAngeles'"
    rec = await send_config_error_alert(store, disp, problem, now=t0)
    assert rec is not None and rec.sent_ok
    msg = rec.message
    assert msg.title == f"ceoscan stopped: configuration problem: {problem}"
    assert msg.priority == "high" and msg.kind == "system"
    assert "exited (code 2)" in msg.body and "not watching X" in msg.body
    # same problem again (a restart loop): suppressed for 24 h
    assert await send_config_error_alert(store, disp, problem, now=t0 + timedelta(hours=1)) is None
    assert len(disp.sent) == 1
    # a different problem alerts; the wording follows ops.config_error_action
    rec2 = await send_config_error_alert(store, disp, "no verified accounts", action="wait", now=t0)
    assert rec2 is not None and "idle" in rec2.message.body and "config_error" in rec2.message.body
    assert len(disp.sent) == 2
    assert all(r["title"].startswith("ceoscan stopped: configuration problem") for r in alert_rows(store, "system"))
    # the marker uses the same kv key/format as app.Service, which clears it once ingest is up
    assert json.loads(store.kv_get(KV_CONFIG_ERROR_ALERT))["hash"]
    store.kv_set(KV_CONFIG_ERROR_ALERT, "")
    assert (await send_config_error_alert(store, disp, "no verified accounts", now=t0)) is not None
    # after 24 h the same problem is alerted again
    later = t0 + timedelta(hours=25)
    assert (await send_config_error_alert(store, disp, "no verified accounts", now=later)) is not None


async def test_config_error_alert_long_message_and_no_store() -> None:
    long_problem = "x" * 400
    raising = FakeDispatcher(raise_exc=RuntimeError("boom"))
    rec = await send_config_error_alert(None, raising, long_problem)  # the database itself may be the problem
    assert rec is not None and not rec.sent_ok  # contained, never raises
    assert rec.message.title.endswith("...") and len(rec.message.title) < 200
    assert long_problem in rec.message.body
