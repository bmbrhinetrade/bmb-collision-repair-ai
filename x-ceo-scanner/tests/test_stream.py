"""Tests for ceoscan.xapi.stream (and the stream probe in ceoscan.xapi.access).

The FilteredStream tests run against a real aiohttp server on 127.0.0.1 that plays X's
filtered-stream endpoint, so chunking, keep-alives and disconnects are exercised for real.
"""

from __future__ import annotations

import asyncio
import json
import random
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from typing import Any

import aiohttp
import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer

from ceoscan import USER_AGENT
from ceoscan.config import RecoverySettings, StreamSettings, XApiSettings
from ceoscan.models import IngestStatus
from ceoscan.xapi.access import AccessReport, check_access, format_access_report
from ceoscan.xapi.client import NotAuthorized, XApiError, error_from_response
from ceoscan.xapi.stream import (
    Backoff,
    FilteredStream,
    IngestEventLog,
    LineBuffer,
    StreamUnavailable,
    classify_stream_errors,
    indicates_no_stream_access,
    stream_params,
)

TOKEN = "test-bearer-token-123456"
STREAM_PATH = "/2/tweets/search/stream"
NOW = datetime(2026, 11, 19, 12, 0, 0, tzinfo=UTC)


# --------------------------------------------------------------------------- Backoff


def test_backoff_network_schedule_linear_and_capped():
    b = Backoff(jitter=0.0)
    delays = [b.next_delay("network") for _ in range(70)]
    assert delays[0] == 0.0  # reconnect immediately the first time after a drop
    assert delays[1:5] == [0.25, 0.5, 0.75, 1.0]
    assert max(delays) == 16.0
    assert delays[-1] == 16.0


def test_backoff_http_schedule_exponential_and_capped():
    b = Backoff(jitter=0.0)
    assert [b.next_delay("http") for _ in range(9)] == [5, 10, 20, 40, 80, 160, 320, 320, 320]


def test_backoff_rate_limit_schedule_exponential_and_capped():
    b = Backoff(jitter=0.0)
    assert [b.next_delay("rate_limit") for _ in range(7)] == [60, 120, 240, 480, 960, 960, 960]


def test_backoff_kinds_escalate_independently_and_reset():
    b = Backoff(jitter=0.0)
    assert b.next_delay("http") == 5
    assert b.next_delay("network") == 0
    assert b.next_delay("http") == 10
    assert b.next_delay("network") == 0.25
    assert b.attempts("http") == 2
    b.reset()
    assert b.attempts("http") == 0
    assert b.next_delay("http") == 5
    assert b.next_delay("network") == 0


def test_backoff_jitter_bounds():
    b = Backoff(jitter=0.2, rng=random.Random(42))
    seen = []
    for _ in range(300):
        b.reset()
        d = b.next_delay("http")
        assert 4.0 <= d <= 6.0
        seen.append(d)
    assert len(set(seen)) > 100  # really jittered
    # network: first attempt stays 0 regardless of jitter; later ones within +-20%
    b.reset()
    assert b.next_delay("network") == 0.0
    for n in range(2, 80):
        base = min(0.25 * (n - 1), 16.0)
        d = b.next_delay("network")
        assert base * 0.8 - 1e-9 <= d <= base * 1.2 + 1e-9
    b.reset()
    for n in range(1, 10):
        base = min(60 * 2 ** (n - 1), 960)
        d = b.next_delay("rate_limit")
        assert base * 0.8 <= d <= base * 1.2


def test_backoff_jitter_is_deterministic_with_seeded_rng():
    a = Backoff(rng=random.Random(7))
    b = Backoff(rng=random.Random(7))
    assert [a.next_delay("http") for _ in range(5)] == [b.next_delay("http") for _ in range(5)]


def test_backoff_reset_at_floor_applied_after_jitter():
    b = Backoff(jitter=0.2, rng=random.Random(1))
    reset_at = NOW + timedelta(seconds=500)
    d = b.next_delay("rate_limit", reset_at=reset_at, now=NOW)
    assert d == 500.0  # 60 s * jitter is below the reset time -> wait until reset
    # a reset time in the past (or sooner than the backoff) does not shorten the backoff
    b2 = Backoff(jitter=0.0)
    assert b2.next_delay("rate_limit", reset_at=NOW + timedelta(seconds=5), now=NOW) == 60.0
    assert b2.next_delay("rate_limit", reset_at=NOW - timedelta(seconds=30), now=NOW) == 120.0
    # floor even beyond the cap
    b3 = Backoff(jitter=0.2)
    assert b3.next_delay("rate_limit", reset_at=NOW + timedelta(seconds=2000), now=NOW) == 2000.0


def test_backoff_rejects_bad_input():
    with pytest.raises(ValueError):
        Backoff(jitter=1.5)
    with pytest.raises(ValueError):
        Backoff().next_delay("bogus")  # type: ignore[arg-type]


# --------------------------------------------------------------------------- LineBuffer


def test_linebuffer_partial_chunks_and_crlf():
    buf = LineBuffer()
    assert buf.feed(b'{"data":{"id"') == []
    assert buf.pending > 0
    assert buf.feed(b':"1"}}\r') == []  # "\r" without "\n" yet
    assert buf.feed(b'\n{"data":{"id":"2"}}\r\n{"da') == [b'{"data":{"id":"1"}}', b'{"data":{"id":"2"}}']
    assert buf.feed(b'ta":{"id":"3"}}\n') == [b'{"data":{"id":"3"}}']
    assert buf.pending == 0


def test_linebuffer_keepalives_are_empty_lines():
    buf = LineBuffer()
    assert buf.feed(b"\r\n") == [b""]
    assert buf.feed(b"\r\n\r\n") == [b"", b""]
    assert buf.feed(b"\r") == []
    assert buf.feed(b"\n") == [b""]
    assert buf.feed(b"") == []


def test_linebuffer_large_line_in_many_chunks():
    big = json.dumps({"data": {"id": "1", "text": "x" * 2_000_000}}).encode()
    buf = LineBuffer()
    out: list[bytes] = []
    for i in range(0, len(big), 4096):
        out += buf.feed(big[i : i + 4096])
    assert out == []
    out += buf.feed(b"\r\n")
    assert out == [big]


# --------------------------------------------------------------------------- pure helpers


def test_stream_params():
    s = XApiSettings()
    p = stream_params(s)
    assert p["tweet.fields"].split(",")[0] == "created_at"
    assert "note_tweet" in p["tweet.fields"]
    assert p["expansions"] == "author_id,referenced_tweets.id,referenced_tweets.id.author_id"
    assert p["user.fields"] == "username,name,verified,public_metrics"
    assert "backfill_minutes" not in p
    s2 = XApiSettings(stream=StreamSettings(backfill_minutes=2))
    assert stream_params(s2)["backfill_minutes"] == "2"


def test_classify_stream_errors():
    op = [{"title": "operational-disconnect", "disconnect_type": "UpstreamOperationalDisconnect",
           "type": "https://api.twitter.com/2/problems/operational-disconnect"}]
    assert classify_stream_errors(op) == "disconnect"
    assert classify_stream_errors([{"title": "ConnectionException", "connection_issue": "TooManyConnections"}]) == (
        "too_many_connections"
    )
    assert classify_stream_errors([{"title": "CreditsDepleted", "type": "https://api.x.com/2/problems/credits"}]) == (
        "credits"
    )
    assert classify_stream_errors([{"title": "Not Found Error", "detail": "Could not find tweet"}]) == "other"
    assert classify_stream_errors("nonsense") == "other"


def test_indicates_no_stream_access():
    not_enrolled = {
        "client_id": "123",
        "detail": "When authenticating requests ... attached to a Project.",
        "reason": "client-not-enrolled",
        "required_enrollment": "Appropriate Level of API Access",
        "title": "Client Forbidden",
        "type": "https://api.twitter.com/2/problems/client-forbidden",
    }
    assert indicates_no_stream_access(error_from_response(403, not_enrolled, {}))
    assert indicates_no_stream_access(error_from_response(403, json.dumps(not_enrolled), {}))
    assert not indicates_no_stream_access(error_from_response(403, {"title": "Forbidden"}, {}))
    assert not indicates_no_stream_access(error_from_response(401, not_enrolled, {}))


# --------------------------------------------------------------------------- fake X stream server


def _post(pid: str, text: str = "hello") -> dict[str, Any]:
    return {
        "data": {"id": pid, "text": text, "author_id": "44196397", "edit_history_tweet_ids": [pid]},
        "includes": {"users": [{"id": "44196397", "username": "elonmusk", "name": "Elon Musk"}]},
        "matching_rules": [{"id": "1", "tag": "ceoscan:acct:r1t0:1"}],
    }


Script = Callable[[web.Request, "FakeX"], Awaitable[web.StreamResponse]]


class FakeX:
    """Plays GET /2/tweets/search/stream; connection N runs scripts[N] (last one repeats)."""

    def __init__(self) -> None:
        self.scripts: list[Script] = []
        self.requests: list[dict[str, Any]] = []
        self.release = asyncio.Event()
        self.base_url = ""

    @property
    def connections(self) -> int:
        return len(self.requests)

    async def handle(self, request: web.Request) -> web.StreamResponse:
        self.requests.append({"headers": dict(request.headers), "query": dict(request.query)})
        script = self.scripts[min(len(self.requests), len(self.scripts)) - 1]
        try:
            return await script(request, self)
        except (ConnectionError, aiohttp.ClientConnectionResetError, RuntimeError):
            return web.Response(status=200)


async def _open(request: web.Request) -> web.StreamResponse:
    resp = web.StreamResponse(status=200, headers={"Content-Type": "application/json"})
    await resp.prepare(request)
    return resp


async def two_posts_then_close(request: web.Request, srv: FakeX) -> web.StreamResponse:
    resp = await _open(request)
    await resp.write(json.dumps(_post("1800000000000000001")).encode() + b"\r\n")
    await resp.write(b"\r\n")  # keep-alive
    raw = json.dumps(_post("1800000000000000002", "second " * 50)).encode()
    await resp.write(raw[:25])  # one post split across chunks
    await asyncio.sleep(0.02)
    await resp.write(raw[25:] + b"\r\n")
    await resp.write(b"\r\n")
    await resp.write_eof()
    return resp


async def keepalives_until_released(request: web.Request, srv: FakeX) -> web.StreamResponse:
    resp = await _open(request)
    while not srv.release.is_set():
        await resp.write(b"\r\n")
        try:
            await asyncio.wait_for(srv.release.wait(), 0.05)
        except TimeoutError:
            pass
    return resp


async def one_heartbeat_then_stall(request: web.Request, srv: FakeX) -> web.StreamResponse:
    resp = await _open(request)
    await resp.write(b"\r\n")
    await srv.release.wait()  # silence: no bytes, connection stays open
    return resp


async def operational_disconnect(request: web.Request, srv: FakeX) -> web.StreamResponse:
    resp = await _open(request)
    await resp.write(json.dumps(_post("1800000000000000010")).encode() + b"\r\n")
    err = {"errors": [{"title": "operational-disconnect", "disconnect_type": "UpstreamOperationalDisconnect",
                       "detail": "This stream has been disconnected upstream for operational reasons.",
                       "type": "https://api.twitter.com/2/problems/operational-disconnect"}]}
    await resp.write(json.dumps(err).encode() + b"\r\n")
    await srv.release.wait()  # X would close; we keep it open to prove the client drops it
    return resp


def json_error(status: int, body: dict[str, Any], headers: dict[str, str] | None = None) -> Script:
    async def script(request: web.Request, srv: FakeX) -> web.StreamResponse:
        return web.json_response(body, status=status, headers=headers or {})

    return script


@pytest.fixture
async def fake_x():
    srv = FakeX()
    app = web.Application()
    app.router.add_get(STREAM_PATH, srv.handle)
    server = TestServer(app, host="127.0.0.1")
    await server.start_server()
    srv.base_url = str(server.make_url("")).rstrip("/")
    try:
        yield srv
    finally:
        srv.release.set()
        await server.close()


@pytest.fixture
async def session():
    async with aiohttp.ClientSession() as s:
        yield s


class RecordingBackoff(Backoff):
    """Records what the stream asked for and returns tiny delays so tests run fast."""

    def __init__(self) -> None:
        super().__init__(jitter=0.0)
        self.calls: list[tuple[str, datetime | None]] = []
        self.resets = 0

    def next_delay(self, kind, reset_at=None, now=None):  # type: ignore[override]
        super().next_delay(kind, reset_at, now)
        self.calls.append((kind, reset_at))
        return 0.01

    def jittered(self, seconds: float) -> float:
        return 0.01

    def reset(self) -> None:
        self.resets += 1
        super().reset()


class FakeClient:
    def __init__(self) -> None:
        self.terminated = 0

    async def terminate_stream_connections(self) -> bool:
        self.terminated += 1
        return True


def make_settings(base_url: str, **stream_kw: Any) -> XApiSettings:
    stream = StreamSettings(**{"heartbeat_timeout_seconds": 5.0, "connect_timeout_seconds": 2.0, **stream_kw})
    return XApiSettings(base_url=base_url, stream=stream, recovery=RecoverySettings(min_gap_seconds=0.0))


async def wait_until(cond: Callable[[], bool], timeout: float = 5.0) -> None:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while not cond():
        if loop.time() > deadline:
            raise AssertionError("condition not met in time")
        await asyncio.sleep(0.01)


async def stop_and_join(stop: asyncio.Event, task: asyncio.Task) -> None:
    stop.set()
    await asyncio.wait_for(task, timeout=5.0)


# --------------------------------------------------------------------------- FilteredStream end-to-end


async def test_stream_end_to_end_posts_heartbeats_and_reconnect(fake_x, session):
    fake_x.scripts = [two_posts_then_close, keepalives_until_released]
    queue: asyncio.Queue = asyncio.Queue()
    status = IngestStatus()
    gaps: list[float] = []

    async def on_gap(gap: float) -> None:
        gaps.append(gap)

    backoff = RecordingBackoff()
    stream = FilteredStream(session, TOKEN, make_settings(fake_x.base_url), queue, status,
                            on_reconnect_gap=on_gap, backoff=backoff)
    stop = asyncio.Event()
    task = asyncio.create_task(stream.run(stop))

    await wait_until(lambda: queue.qsize() == 2 and fake_x.connections >= 2 and status.state == "connected")
    items = [queue.get_nowait() for _ in range(2)]
    assert [i.payload["data"]["id"] for i in items] == ["1800000000000000001", "1800000000000000002"]
    assert all(i.source == "stream" for i in items)
    assert items[1].payload["data"]["text"].startswith("second")
    assert items[0].payload["matching_rules"][0]["tag"] == "ceoscan:acct:r1t0:1"
    assert all(i.received_at.tzinfo is not None for i in items)

    assert status.mode == "stream"
    assert status.reconnects == 1
    assert status.connected_since is not None
    assert status.down_since is None  # cleared on reconnect
    assert status.last_heartbeat_at is not None
    assert status.last_error == "stream closed by server"
    # healthy first connection (delivered data) -> reset -> immediate network reconnect
    assert backoff.resets >= 1
    assert backoff.calls[0][0] == "network"
    await wait_until(lambda: len(gaps) == 1)  # gap recovery ran in the background
    assert gaps[0] >= 0

    # request details: auth + user agent + fields, never anything else
    req = fake_x.requests[0]
    assert req["headers"]["Authorization"] == f"Bearer {TOKEN}"
    assert req["headers"]["User-Agent"] == USER_AGENT
    assert req["query"]["expansions"].startswith("author_id")
    assert "tweet.fields" in req["query"] and "user.fields" in req["query"]

    # heartbeats keep updating while connected
    hb1 = status.last_heartbeat_at
    await wait_until(lambda: status.last_heartbeat_at != hb1)

    await stop_and_join(stop, task)
    assert status.state == "stopped"
    assert status.connected_since is None
    assert stream.connections == 2 and stream.posts_received == 2


async def test_stream_gap_recovery_not_triggered_for_short_gaps(fake_x, session):
    fake_x.scripts = [two_posts_then_close, keepalives_until_released]
    settings = make_settings(fake_x.base_url)
    settings.recovery.min_gap_seconds = 60.0
    called: list[float] = []

    async def on_gap(gap: float) -> None:
        called.append(gap)

    status = IngestStatus()
    stream = FilteredStream(session, TOKEN, settings, asyncio.Queue(), status,
                            on_reconnect_gap=on_gap, backoff=RecordingBackoff())
    stop = asyncio.Event()
    task = asyncio.create_task(stream.run(stop))
    await wait_until(lambda: status.reconnects == 1 and status.state == "connected")
    await asyncio.sleep(0.05)
    assert called == []
    await stop_and_join(stop, task)


async def test_stream_heartbeat_timeout_triggers_reconnect(fake_x, session):
    fake_x.scripts = [one_heartbeat_then_stall, keepalives_until_released]
    status = IngestStatus()
    backoff = RecordingBackoff()
    gaps: list[float] = []

    async def on_gap(gap: float) -> None:
        gaps.append(gap)

    stream = FilteredStream(session, TOKEN, make_settings(fake_x.base_url, heartbeat_timeout_seconds=0.3),
                            asyncio.Queue(), status, on_reconnect_gap=on_gap, backoff=backoff)
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    t0 = loop.time()
    task = asyncio.create_task(stream.run(stop))
    await wait_until(lambda: fake_x.connections >= 2 and status.state == "connected", timeout=5.0)
    elapsed = loop.time() - t0
    assert 0.25 <= elapsed < 3.0
    assert "heartbeat timeout" in (status.last_error or "")
    assert status.reconnects == 1
    assert backoff.calls and backoff.calls[0][0] == "network"
    # the gap is measured from the last bytes received, so it covers the silent period
    await wait_until(lambda: len(gaps) == 1)
    assert gaps[0] >= 0.25
    # keep-alives on the second connection keep it alive well past the heartbeat timeout
    await asyncio.sleep(0.5)
    assert fake_x.connections == 2
    await stop_and_join(stop, task)


async def test_stream_operational_disconnect_reconnects(fake_x, session):
    fake_x.scripts = [operational_disconnect, keepalives_until_released]
    queue: asyncio.Queue = asyncio.Queue()
    status = IngestStatus()
    stream = FilteredStream(session, TOKEN, make_settings(fake_x.base_url), queue, status,
                            backoff=RecordingBackoff())
    stop = asyncio.Event()
    task = asyncio.create_task(stream.run(stop))
    await wait_until(lambda: fake_x.connections >= 2 and status.state == "connected")
    assert queue.qsize() == 1
    assert "operational-disconnect" in (status.last_error or "")
    await stop_and_join(stop, task)


async def test_stream_402_sets_credits_depleted_and_waits(fake_x, session):
    body = {"title": "CreditsDepleted", "detail": "Your enrolled account does not have any credits.",
            "type": "https://api.twitter.com/2/problems/credits"}
    fake_x.scripts = [json_error(402, body)]
    status = IngestStatus()
    stream = FilteredStream(session, TOKEN,
                            make_settings(fake_x.base_url, credits_depleted_retry_seconds=60.0),
                            asyncio.Queue(), status, backoff=RecordingBackoff())
    stop = asyncio.Event()
    task = asyncio.create_task(stream.run(stop))
    await wait_until(lambda: status.state == "credits_depleted")
    await asyncio.sleep(0.1)
    assert fake_x.connections == 1  # waiting credits_depleted_retry_seconds, not hammering X
    assert status.down_since is not None
    assert status.connected_since is None
    assert "402" in (status.last_error or "") and "CreditsDepleted" in status.last_error
    assert TOKEN not in status.last_error
    await stop_and_join(stop, task)  # stop interrupts the long wait
    assert status.state == "stopped"


async def test_stream_402_retries_after_configured_wait(fake_x, session):
    fake_x.scripts = [json_error(402, {"title": "Payment Required", "detail": "credits depleted", "status": 402}),
                      keepalives_until_released]
    status = IngestStatus()
    stream = FilteredStream(session, TOKEN,
                            make_settings(fake_x.base_url, credits_depleted_retry_seconds=0.1),
                            asyncio.Queue(), status, backoff=RecordingBackoff())
    stop = asyncio.Event()
    task = asyncio.create_task(stream.run(stop))
    await wait_until(lambda: status.state == "connected")
    assert fake_x.connections == 2
    assert status.down_since is None
    await stop_and_join(stop, task)


async def test_stream_403_no_access_raises_stream_unavailable(fake_x, session):
    body = {"title": "Client Forbidden", "reason": "client-not-enrolled",
            "required_enrollment": "Appropriate Level of API Access",
            "type": "https://api.twitter.com/2/problems/client-forbidden",
            "detail": "When authenticating requests to the Twitter API v2 endpoints ..."}
    fake_x.scripts = [json_error(403, body)]
    status = IngestStatus()
    stream = FilteredStream(session, TOKEN, make_settings(fake_x.base_url), asyncio.Queue(), status,
                            backoff=RecordingBackoff())
    with pytest.raises(StreamUnavailable):
        await asyncio.wait_for(stream.run(asyncio.Event()), timeout=5.0)
    assert status.state == "down"
    assert "403" in (status.last_error or "")


async def test_stream_repeated_generic_403_eventually_unavailable(fake_x, session):
    fake_x.scripts = [json_error(403, {"title": "Forbidden", "detail": "nope"})]
    backoff = RecordingBackoff()
    stream = FilteredStream(session, TOKEN, make_settings(fake_x.base_url), asyncio.Queue(), IngestStatus(),
                            backoff=backoff)
    with pytest.raises(StreamUnavailable):
        await asyncio.wait_for(stream.run(asyncio.Event()), timeout=5.0)
    assert fake_x.connections == 3
    assert [k for k, _ in backoff.calls] == ["http", "http"]


async def test_stream_429_uses_rate_limit_backoff_with_reset_header(fake_x, session):
    reset = int(NOW.timestamp()) + 600
    fake_x.scripts = [json_error(429, {"title": "Too Many Requests", "detail": "Too Many Requests"},
                                 {"x-rate-limit-reset": str(reset), "x-rate-limit-remaining": "0"}),
                      keepalives_until_released]
    backoff = RecordingBackoff()
    status = IngestStatus()
    stream = FilteredStream(session, TOKEN, make_settings(fake_x.base_url), asyncio.Queue(), status,
                            backoff=backoff)
    stop = asyncio.Event()
    task = asyncio.create_task(stream.run(stop))
    await wait_until(lambda: status.state == "connected")
    assert backoff.calls[0] == ("rate_limit", datetime.fromtimestamp(reset, tz=UTC))
    await stop_and_join(stop, task)


async def test_stream_429_real_backoff_waits_until_reset(fake_x, session):
    reset_at = datetime.now(UTC) + timedelta(seconds=900)
    fake_x.scripts = [json_error(429, {"title": "Too Many Requests"},
                                 {"x-rate-limit-reset": str(int(reset_at.timestamp()))})]
    status = IngestStatus()
    stream = FilteredStream(session, TOKEN, make_settings(fake_x.base_url), asyncio.Queue(), status)
    stop = asyncio.Event()
    task = asyncio.create_task(stream.run(stop))
    await wait_until(lambda: status.state == "backoff")
    assert stream.backoff.attempts("rate_limit") == 1
    await asyncio.sleep(0.1)
    assert fake_x.connections == 1
    await stop_and_join(stop, task)


async def test_stream_too_many_connections_terminates_then_retries(fake_x, session):
    body = {"title": "ConnectionException", "connection_issue": "TooManyConnections",
            "detail": "This stream is currently at the maximum allowed connection limit.",
            "type": "https://api.twitter.com/2/problems/streaming-connection"}
    fake_x.scripts = [json_error(429, body), keepalives_until_released]
    client = FakeClient()
    status = IngestStatus()
    stream = FilteredStream(session, TOKEN, make_settings(fake_x.base_url), asyncio.Queue(), status,
                            client=client, backoff=RecordingBackoff())  # type: ignore[arg-type]
    stop = asyncio.Event()
    task = asyncio.create_task(stream.run(stop))
    await wait_until(lambda: status.state == "connected")
    assert client.terminated == 1
    await stop_and_join(stop, task)


async def test_stream_401_logs_and_uses_http_backoff(fake_x, session, caplog):
    fake_x.scripts = [json_error(401, {"title": "Unauthorized", "detail": "Unauthorized", "status": 401}),
                      keepalives_until_released]
    backoff = RecordingBackoff()
    status = IngestStatus()
    stream = FilteredStream(session, TOKEN, make_settings(fake_x.base_url), asyncio.Queue(), status,
                            backoff=backoff)
    stop = asyncio.Event()
    with caplog.at_level("DEBUG"):
        task = asyncio.create_task(stream.run(stop))
        await wait_until(lambda: status.state == "connected")
        await stop_and_join(stop, task)
    assert backoff.calls[0][0] == "http"
    assert TOKEN not in caplog.text  # never log the bearer token


async def test_stream_network_error_uses_network_backoff(session):
    # nothing listens on this port -> connection refused
    srv = TestServer(web.Application(), host="127.0.0.1")
    await srv.start_server()
    url = str(srv.make_url("")).rstrip("/")
    await srv.close()
    backoff = RecordingBackoff()
    status = IngestStatus()
    stream = FilteredStream(session, TOKEN, make_settings(url), asyncio.Queue(), status, backoff=backoff)
    stop = asyncio.Event()
    task = asyncio.create_task(stream.run(stop))
    await wait_until(lambda: len(backoff.calls) >= 3)
    await stop_and_join(stop, task)
    assert {k for k, _ in backoff.calls} == {"network"}
    assert status.down_since is not None
    assert status.last_error


async def test_stream_stop_while_connected_is_prompt(fake_x, session):
    fake_x.scripts = [keepalives_until_released]
    status = IngestStatus()
    stream = FilteredStream(session, TOKEN, make_settings(fake_x.base_url), asyncio.Queue(), status)
    stop = asyncio.Event()
    task = asyncio.create_task(stream.run(stop))
    await wait_until(lambda: status.state == "connected")
    loop = asyncio.get_running_loop()
    t0 = loop.time()
    await stop_and_join(stop, task)
    assert loop.time() - t0 < 1.0
    assert status.state == "stopped"


async def test_stream_full_queue_pauses_reads_without_heartbeat_timeout(fake_x, session):
    fake_x.scripts = [two_posts_then_close, keepalives_until_released]
    queue: asyncio.Queue = asyncio.Queue(maxsize=1)
    status = IngestStatus()
    stream = FilteredStream(session, TOKEN, make_settings(fake_x.base_url, heartbeat_timeout_seconds=0.2),
                            queue, status, backoff=RecordingBackoff())
    stop = asyncio.Event()
    task = asyncio.create_task(stream.run(stop))
    await wait_until(lambda: queue.full())
    await asyncio.sleep(0.5)  # longer than the heartbeat timeout while blocked on the queue
    assert fake_x.connections == 1
    first = queue.get_nowait()
    await wait_until(lambda: queue.qsize() == 1)
    second = queue.get_nowait()
    assert [first.payload["data"]["id"], second.payload["data"]["id"]] == [
        "1800000000000000001", "1800000000000000002"]
    await stop_and_join(stop, task)


# --------------------------------------------------------------------------- watchdog / catch-up / TMC / events


async def test_fresh_connection_going_silent_is_dropped_after_heartbeat_not_connect_timeout(fake_x, session):
    """ING-4: the idle limit shrinks from connect_timeout + heartbeat to heartbeat once the headers
    arrive; the watchdog must notice, so a silent fresh connection is dropped ~heartbeat later."""
    fake_x.scripts = [one_heartbeat_then_stall, keepalives_until_released]
    status = IngestStatus()
    stream = FilteredStream(session, TOKEN,
                            make_settings(fake_x.base_url, heartbeat_timeout_seconds=0.3, connect_timeout_seconds=10.0),
                            asyncio.Queue(), status, backoff=RecordingBackoff())
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    t0 = loop.time()
    task = asyncio.create_task(stream.run(stop))
    await wait_until(lambda: fake_x.connections >= 2, timeout=8.0)
    assert loop.time() - t0 < 2.5  # ~0.3 s (+ <= 1 s watchdog granularity), not 10.3 s
    assert "heartbeat timeout" in (status.last_error or "")
    await stop_and_join(stop, task)


async def test_first_connect_catch_up_runs_regardless_of_min_gap(fake_x, session):
    """ING-5 / SEC-5: with catch_up_on_first_connect the first connection of a process runs gap
    recovery (posts made while ceoscan was restarting), reporting the seeded gap."""
    fake_x.scripts = [keepalives_until_released]
    settings = make_settings(fake_x.base_url)
    settings.recovery.min_gap_seconds = 3600.0
    gaps: list[float] = []
    connected: list[int] = []

    async def on_gap(gap: float) -> None:
        gaps.append(gap)

    status = IngestStatus()
    stream = FilteredStream(session, TOKEN, settings, asyncio.Queue(), status, on_reconnect_gap=on_gap,
                            backoff=RecordingBackoff(), catch_up_on_first_connect=True,
                            gap_started_at=datetime.now(UTC) - timedelta(seconds=90),
                            on_connected=lambda: connected.append(1))
    stop = asyncio.Event()
    task = asyncio.create_task(stream.run(stop))
    await wait_until(lambda: len(gaps) == 1)
    assert 89 <= gaps[0] <= 95
    assert connected == [1]
    await stop_and_join(stop, task)


async def test_first_connect_without_catch_up_runs_no_recovery(fake_x, session):
    fake_x.scripts = [keepalives_until_released]
    gaps: list[float] = []

    async def on_gap(gap: float) -> None:
        gaps.append(gap)

    status = IngestStatus()
    stream = FilteredStream(session, TOKEN, make_settings(fake_x.base_url), asyncio.Queue(), status,
                            on_reconnect_gap=on_gap, backoff=RecordingBackoff())
    stop = asyncio.Event()
    task = asyncio.create_task(stream.run(stop))
    await wait_until(lambda: status.state == "connected")
    await asyncio.sleep(0.05)
    assert gaps == []
    await stop_and_join(stop, task)


async def test_short_reconnect_still_recovers_because_of_delivery_margin(fake_x, session):
    """ING-7: a reconnect after ~0 s still has posts in flight (X's ~5 s delivery lag)."""
    fake_x.scripts = [two_posts_then_close, keepalives_until_released]
    settings = make_settings(fake_x.base_url)
    settings.recovery.min_gap_seconds = 4.0
    gaps: list[float] = []
    snapshots: list[int] = []

    async def on_gap(gap: float) -> None:
        gaps.append(gap)

    status = IngestStatus()
    stream = FilteredStream(session, TOKEN, settings, asyncio.Queue(), status, on_reconnect_gap=on_gap,
                            backoff=RecordingBackoff(), on_gap_start=lambda: snapshots.append(1))
    stop = asyncio.Event()
    task = asyncio.create_task(stream.run(stop))
    await wait_until(lambda: len(gaps) == 1)
    assert gaps[0] < 2.0  # measured gap is short ...
    assert snapshots == [1]  # ... and the cursors were snapshotted once when it began
    await stop_and_join(stop, task)


async def test_on_gap_start_fires_once_per_outage(session):
    srv = TestServer(web.Application(), host="127.0.0.1")
    await srv.start_server()
    url = str(srv.make_url("")).rstrip("/")
    await srv.close()  # connection refused on every attempt
    snapshots: list[int] = []
    backoff = RecordingBackoff()
    stream = FilteredStream(session, TOKEN, make_settings(url), asyncio.Queue(), IngestStatus(), backoff=backoff,
                            on_gap_start=lambda: snapshots.append(1))
    stop = asyncio.Event()
    task = asyncio.create_task(stream.run(stop))
    await wait_until(lambda: len(backoff.calls) >= 4)
    await stop_and_join(stop, task)
    assert snapshots == [1]


class _NoClient:
    async def terminate_stream_connections(self) -> bool:
        return True


def _tmc(remaining: str) -> tuple[XApiError, dict[str, str]]:
    body = {"title": "ConnectionException", "detail": "max allowed connection limit",
            "connection_issue": "TooManyConnections"}
    hdr = {"x-rate-limit-limit": "50", "x-rate-limit-remaining": remaining,
           "x-rate-limit-reset": str(int(datetime.now(UTC).timestamp()) + 800)}
    return error_from_response(429, body, hdr), hdr


async def test_too_many_connections_waits_for_reset_only_when_connects_are_used_up():
    """ING-8: after the fast retries, wait for the connect window's reset only if remaining == 0."""
    s = XApiSettings()
    fs = FilteredStream(None, "t", s, asyncio.Queue(), IngestStatus(), client=_NoClient(),  # type: ignore[arg-type]
                        backoff=Backoff(jitter=0.0))
    delays = []
    for _ in range(5):
        err, hdr = _tmc("45")
        delays.append((await fs._on_http_error(err, hdr))[0])
    assert delays[:3] == [5.0, 5.0, 5.0]  # fast retries after terminating our stale connection
    assert delays[3] == 60.0 and delays[4] == 120.0  # plain rate-limit backoff, not ~800 s
    fs2 = FilteredStream(None, "t", s, asyncio.Queue(), IngestStatus(), client=_NoClient(),  # type: ignore[arg-type]
                         backoff=Backoff(jitter=0.0))
    for _ in range(3):
        err, hdr = _tmc("0")
        await fs2._on_http_error(err, hdr)
    err, hdr = _tmc("0")
    assert (await fs2._on_http_error(err, hdr))[0] >= 790  # budget exhausted: wait for the reset


class _EventStore:
    def __init__(self, fail: bool = False) -> None:
        self.rows: list[tuple[str, str, str]] = []
        self.fail = fail

    def log_event(self, kind: str, detail: str = "", level: str = "info") -> None:
        if self.fail:
            raise RuntimeError("disk full")
        self.rows.append((kind, detail, level))


def test_ingest_event_log_throttles_per_kind_and_counts_repeats():
    t = [0.0]
    store = _EventStore()
    ev = IngestEventLog(store, interval=60.0, clock=lambda: t[0])
    assert ev.error("stream_error", "HTTP 503 Service Unavailable") is True
    assert ev.error("stream_error", "HTTP 503 again") is False
    assert ev.error("poll_error", "timeout") is True  # another kind is not throttled
    t[0] = 30.0
    assert ev.error("stream_error", "still down") is False
    t[0] = 61.0
    assert ev.error("stream_error", "disconnected by X: operational-disconnect") is True
    assert [(k, lvl) for k, _, lvl in store.rows] == [("stream_error", "error"), ("poll_error", "error"),
                                                      ("stream_error", "error")]
    assert store.rows[2][1].endswith("(+2 more since the previous stream_error event)")
    assert IngestEventLog(_EventStore(fail=True)).error("stream_error", "x") is False  # never raises
    assert IngestEventLog(None).error("stream_error", "x") is False


async def test_stream_disconnects_are_recorded_as_error_events(fake_x, session):
    fake_x.scripts = [two_posts_then_close, keepalives_until_released]
    store = _EventStore()
    status = IngestStatus()
    stream = FilteredStream(session, TOKEN, make_settings(fake_x.base_url), asyncio.Queue(), status,
                            backoff=RecordingBackoff(), events=IngestEventLog(store))
    stop = asyncio.Event()
    task = asyncio.create_task(stream.run(stop))
    await wait_until(lambda: status.reconnects == 1 and status.state == "connected")
    await stop_and_join(stop, task)
    assert store.rows == [("stream_error", "stream closed by server", "error")]


# --------------------------------------------------------------------------- access check (uses the stream probe)


class AccessClient:
    def __init__(self, rules: list[dict] | Exception, usage: dict | Exception) -> None:
        self._rules = rules
        self._usage = usage

    async def get_rules(self) -> list[dict]:
        if isinstance(self._rules, Exception):
            raise self._rules
        return self._rules

    async def get_usage(self) -> dict:
        if isinstance(self._usage, Exception):
            raise self._usage
        return self._usage


USAGE = {"cap_reset_day": 19, "project_cap": "3000000", "project_id": "1", "project_usage": "1234"}


async def test_check_access_credits_depleted_picks_stream(fake_x, session):
    fake_x.scripts = [json_error(402, {"title": "CreditsDepleted", "type": "https://api.twitter.com/2/problems/credits"})]
    client = AccessClient([{"id": "1", "value": "from:1", "tag": "ceoscan:acct:r1t0:1"}], USAGE)
    r = await check_access(client, make_settings(fake_x.base_url), 19, session, TOKEN)  # type: ignore[arg-type]
    assert isinstance(r, AccessReport)
    assert r.mode == "stream" and r.stream_available is True and r.credits_ok is False
    assert r.existing_rules == 1 and r.usage == USAGE
    assert r.est_latency_s == (4.5, 8.0)
    assert r.poll_interval_s == pytest.approx(19 * 900 / 8000)
    text = format_access_report(r)
    assert "DEPLETED" in text and "1,234 / 3,000,000" in text
    assert TOKEN not in text
    assert fake_x.requests[0]["query"] == {}  # probe sends no params (no backfill)


async def test_check_access_no_stream_falls_back_to_poll(fake_x, session):
    fake_x.scripts = [json_error(403, {"title": "Client Forbidden", "reason": "client-not-enrolled"})]
    client = AccessClient(NotAuthorized(status=403, title="Forbidden"), USAGE)
    r = await check_access(client, make_settings(fake_x.base_url), 19, session, TOKEN)  # type: ignore[arg-type]
    assert r.mode == "poll" and r.stream_available is False
    assert r.est_latency_s == pytest.approx((19 * 900 / 8000 / 2 + 2, 19 * 900 / 8000 + 2))
    assert any("not available on this plan" in m for m in r.messages)
    assert "falling back" in format_access_report(r)


async def test_check_access_ok_and_forced_modes(fake_x, session):
    fake_x.scripts = [keepalives_until_released]
    client = AccessClient([], XApiError(status=500, title="Internal Server Error"))
    s = make_settings(fake_x.base_url)
    r = await check_access(client, s, 3, session, TOKEN)  # type: ignore[arg-type]
    assert r.mode == "stream" and r.stream_available is True and r.credits_ok is True
    assert r.usage is None and r.existing_rules == 0
    s.mode = "poll"
    r2 = await check_access(client, s, 3, session, TOKEN)  # type: ignore[arg-type]
    assert r2.mode == "poll" and r2.poll_interval_s == 2.0 and r2.est_latency_s == (3.0, 4.0)


async def test_check_access_busy_stream(fake_x, session):
    fake_x.scripts = [json_error(429, {"title": "ConnectionException", "connection_issue": "TooManyConnections"})]
    r = await check_access(AccessClient([], USAGE), make_settings(fake_x.base_url), 2, session, TOKEN)  # type: ignore[arg-type]
    assert r.mode == "stream" and r.stream_available is True and r.credits_ok is None
    assert any("busy" in m for m in r.messages)


async def test_check_access_reports_a_rejected_bearer_token(fake_x, session):
    """SEC-10: a 401 anywhere sets auth_ok=False, so `check-access` exits 2 instead of 0."""
    from ceoscan.__main__ import EXIT_CONFIG, access_exit_code

    fake_x.scripts = [json_error(401, {"title": "Unauthorized", "type": "about:blank", "status": 401})]
    unauthorized = NotAuthorized(status=401, title="Unauthorized")
    r = await check_access(AccessClient(unauthorized, unauthorized), make_settings(fake_x.base_url), 3, session, TOKEN)  # type: ignore[arg-type]
    assert r.auth_ok is False and r.credits_ok is None
    assert "REJECTED (HTTP 401)" in format_access_report(r)
    assert access_exit_code(r) == EXIT_CONFIG


async def test_check_access_accepted_token_sets_auth_ok(fake_x, session):
    fake_x.scripts = [keepalives_until_released]
    r = await check_access(AccessClient([], USAGE), make_settings(fake_x.base_url), 3, session, TOKEN)  # type: ignore[arg-type]
    assert r.auth_ok is True
    assert "bearer token:     accepted" in format_access_report(r)
