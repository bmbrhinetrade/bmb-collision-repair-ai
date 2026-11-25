"""End-to-end tests of the ceoscan service against a fake X API (and a fake ntfy server).

* In-process tests (always run, fast): ``ceoscan.app.Service`` against the fake X API on
  127.0.0.1 with a capturing alert channel - the startup alert only once ingest is up, a
  configuration problem (one deduplicated alert; exit 2, or stay idle with /health state
  ``config_error``), the restart catch-up in stream mode (age floor, last-alive baseline), the
  last-alive kv, the Claude warm-up, ``ops.pipeline_workers``, stream error events and polling that
  re-paces itself to X's real rate limit.
* Subprocess scenarios (slow, ~30 s each; run only with ``CEOSCAN_E2E=1``): the real service
  (``python -m ceoscan run``) against the fake X API and a fake ntfy server - stream mode with
  forced reconnects (TCP drop, TooManyConnections, a stream that goes silent) and gap recovery
  that paginates, and the polling fallback. They check alerts and their fields, the latency log
  line of every alert, /health, and a clean exit 0 on SIGTERM.

Nothing touches the real network; config, watchlist and the SQLite DB live in tmp_path.
"""

from __future__ import annotations

import asyncio
import contextlib
import itertools
import json
import logging
import os
import signal
import socket
import sqlite3
import sys
import time
from collections.abc import Awaitable, Callable
from datetime import timedelta
from pathlib import Path
from typing import Any

import aiohttp
import pytest
from aiohttp import web

import ceoscan.app as app_mod
from ceoscan.alerts.claude_desk import BRIEF_LINE, ClaudeDesk
from ceoscan.alerts.dispatcher import AlertDispatcher
from ceoscan.alerts.notifiers import CaptureNotifier
from ceoscan.app import (
    CONFIG_ALERT_TITLE,
    EXIT_CONFIG,
    EXIT_OK,
    KV_CONFIG_ALERT,
    KV_LAST_ALIVE,
    Service,
)
from ceoscan.config import SECRET_ENV_VARS, AppConfig, Secrets, Settings
from ceoscan.logging_setup import RedactingFilter
from ceoscan.models import Account, Watchlist
from ceoscan.scoring.claude import OfflineScorer
from ceoscan.store import Store
from ceoscan.timeutil import datetime_to_snowflake, now_utc

ROOT = Path(__file__).resolve().parents[1]
TOKEN = "e2e-bearer-token-123456"
NTFY_TOPIC = "e2e-topic-abcdef123"
TWITTER_EPOCH_MS = 1288834974657
E2E = os.environ.get("CEOSCAN_E2E") == "1"
needs_e2e = pytest.mark.skipif(not E2E, reason="slow subprocess end-to-end test; set CEOSCAN_E2E=1 to run it")

ELON, AMD, SAYLOR, NVIDIA, BIGMACRO = "1000000001", "1000000002", "1000000003", "1000000004", "2000000001"
USERS: dict[str, dict[str, Any]] = {
    ELON: {"id": ELON, "username": "elonmusk", "name": "Elon Musk", "verified": True,
           "public_metrics": {"followers_count": 230_000_000}},
    AMD: {"id": AMD, "username": "AMD", "name": "AMD", "verified": True,
          "public_metrics": {"followers_count": 2_000_000}},
    SAYLOR: {"id": SAYLOR, "username": "saylor", "name": "Michael Saylor", "verified": True,
             "public_metrics": {"followers_count": 4_500_000}},
    NVIDIA: {"id": NVIDIA, "username": "nvidia", "name": "NVIDIA", "verified": True,
             "public_metrics": {"followers_count": 3_000_000}},
    BIGMACRO: {"id": BIGMACRO, "username": "bigmacro", "name": "Big Macro", "verified": True,
               "public_metrics": {"followers_count": 5_000_000}},
}

FORBIDDEN = {
    "client_id": "1",
    "detail": "When authenticating requests to the Twitter API v2 endpoints, you must use keys and tokens "
    "from a Twitter developer App that is attached to a Project.",
    "registration_url": "https://developer.x.com/",
    "title": "Client Forbidden",
    "required_enrollment": "Appropriate Level of API Access",
    "reason": "client-not-enrolled",
    "type": "https://api.twitter.com/2/problems/client-forbidden",
}

_seq = itertools.count(1)


def snowflake(ms: int | None = None) -> str:
    if ms is None:
        ms = int(time.time() * 1000)
    return str(((ms - TWITTER_EPOCH_MS) << 22) | (next(_seq) & 0x3FFFFF))


def ms_ago(seconds: float) -> int:
    return int((time.time() - seconds) * 1000)


def make_post(author: str, text: str, *, cashtags: tuple[str, ...] = (), ref: dict | None = None,
              ms: int | None = None, rule_tag: str = "ceoscan:acct:r1t0:1") -> dict[str, Any]:
    pid = snowflake(ms)
    created = time.strftime("%Y-%m-%dT%H:%M:%S.000Z", time.gmtime(((int(pid) >> 22) + TWITTER_EPOCH_MS) / 1000))
    data: dict[str, Any] = {
        "id": pid, "text": text, "author_id": author, "created_at": created, "edit_history_tweet_ids": [pid],
        "conversation_id": pid, "lang": "en",
        "public_metrics": {"retweet_count": 0, "reply_count": 0, "like_count": 0, "quote_count": 0},
    }
    if cashtags:
        data["entities"] = {"cashtags": [{"start": max(text.find("$" + t), 0), "end": max(text.find("$" + t), 0)
                                          + len(t) + 1, "tag": t} for t in cashtags]}
    if ref:
        data["referenced_tweets"] = [ref]
    return {"data": data, "includes": {"users": [USERS[author]]},
            "matching_rules": [{"id": "1700000000000000001", "tag": rule_tag}]}


def pid_of(payload: dict[str, Any]) -> str:
    return str(payload["data"]["id"])


# --------------------------------------------------------------------------- fake X API

StreamScript = Callable[["FakeX", web.Request], Awaitable[web.StreamResponse]]
_DROPPED = (ConnectionError, aiohttp.ClientConnectionResetError, RuntimeError, asyncio.CancelledError)


async def open_stream(request: web.Request) -> web.StreamResponse:
    resp = web.StreamResponse(status=200, headers={"Content-Type": "application/json"})
    await resp.prepare(request)
    return resp


async def keepalives(fx: FakeX, request: web.Request) -> web.StreamResponse:
    """X's normal idle stream: a keep-alive every ``fx.keepalive_every`` seconds."""
    resp = await open_stream(request)
    with contextlib.suppress(*_DROPPED):
        while not fx.release.is_set():
            await resp.write(b"\r\n")
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(fx.release.wait(), fx.keepalive_every)
    return resp


async def drop_then_end(fx: FakeX, request: web.Request) -> web.StreamResponse:
    resp = await open_stream(request)
    await resp.write(b"\r\n")
    await resp.write_eof()  # X closed the stream
    return resp


class FakeX:
    """Plays the X API v2 endpoints ceoscan uses. Real stream connection N runs stream_scripts[N-1]
    (the last one repeats); the access-check probe (no query parameters) is answered separately."""

    def __init__(self) -> None:
        self.rules: list[dict[str, Any]] = [
            {"id": "111", "value": "from:999 -is:retweet", "tag": "ceoscan:acct:r0t0:9"},  # stale, ours
            {"id": "222", "value": "from:12345 lang:en", "tag": "someone-else"},  # foreign: kept
        ]
        self.next_rule_id = 1000
        self.reject_rules = False
        self.no_stream = False  # a plan without the filtered stream: rules, probe and stream answer 403
        self.stream_scripts: list[StreamScript] = [keepalives]
        self.keepalive_every = 0.05
        self.timelines: dict[str, list[dict[str, Any]]] = {uid: [] for uid in USERS}
        self.rate_limit_limit: int | None = None
        self.log: list[dict[str, Any]] = []
        self.events: list[str] = []
        self.stream_conns: list[float] = []  # time.monotonic() of each real stream connection
        self.release = asyncio.Event()
        self.t0 = time.monotonic()
        self.missed: dict[str, str] = {}

    def ev(self, msg: str) -> None:
        self.events.append(f"{time.monotonic() - self.t0:6.2f}s {msg}")

    def app(self) -> web.Application:
        app = web.Application(middlewares=[self._mw])
        app.router.add_get("/2/tweets/search/stream/rules", self.get_rules)
        app.router.add_post("/2/tweets/search/stream/rules", self.post_rules)
        app.router.add_get("/2/usage/tweets", self.usage)
        app.router.add_delete("/2/connections/filtered_stream", self.delete_conns)
        app.router.add_get("/2/users/{uid}/tweets", self.timeline)
        app.router.add_get("/2/tweets/search/stream", self.stream)
        return app

    @web.middleware
    async def _mw(self, request: web.Request, handler: Any) -> web.StreamResponse:
        self.log.append({"t": round(time.monotonic() - self.t0, 2), "method": request.method, "path": request.path,
                         "query": dict(request.query), "auth": request.headers.get("Authorization")})
        return await handler(request)

    def timeline_requests(self, uid: str | None = None) -> list[dict[str, Any]]:
        return [r["query"] | {"uid": r["path"].split("/")[3]} for r in self.log
                if r["path"].startswith("/2/users/") and r["path"].endswith("/tweets")
                and (uid is None or r["path"] == f"/2/users/{uid}/tweets")]

    async def get_rules(self, request: web.Request) -> web.Response:
        if self.no_stream:
            return web.json_response(FORBIDDEN, status=403)
        return web.json_response({"data": self.rules, "meta": {"result_count": len(self.rules)}}
                                 if self.rules else {"meta": {"result_count": 0}})

    async def post_rules(self, request: web.Request) -> web.Response:
        body = await request.json()
        dry = request.query.get("dry_run") == "true"
        if "add" in body:
            adds = body["add"]
            self.ev(f"rules add dry_run={dry} n={len(adds)}")
            if self.reject_rules:
                errors = [{"value": r["value"], "id": "x", "title": "UnprocessableEntity",
                           "type": "https://api.twitter.com/2/problems/invalid-rules",
                           "detail": "Rule contains an operator this plan does not support"} for r in adds]
                return web.json_response({"errors": errors, "meta": {"summary": {
                    "created": 0, "not_created": len(adds), "valid": 0, "invalid": len(adds)}}})
            created = []
            for r in adds:
                created.append({"id": str(self.next_rule_id), "value": r["value"], "tag": r.get("tag")})
                self.next_rule_id += 1
            if not dry:
                self.rules.extend(created)
            return web.json_response({"data": created, "meta": {"summary": {
                "created": 0 if dry else len(created), "not_created": 0, "valid": len(created), "invalid": 0}}})
        ids = set(body.get("delete", {}).get("ids", []))
        self.ev(f"rules delete dry_run={dry} ids={sorted(ids)}")
        if not dry:
            self.rules = [r for r in self.rules if r["id"] not in ids]
        return web.json_response({"meta": {"summary": {"deleted": len(ids), "not_deleted": 0}}})

    async def usage(self, request: web.Request) -> web.Response:
        return web.json_response({"data": {"cap_reset_day": 19, "project_cap": "3000000", "project_id": "1",
                                           "project_usage": "1234"}})

    async def delete_conns(self, request: web.Request) -> web.Response:
        self.ev("DELETE /2/connections/filtered_stream")
        return web.json_response({"data": {"killed_connections": True}})

    async def timeline(self, request: web.Request) -> web.Response:
        """Newest first; honours since_id, max_results, exclude and pagination_token (an offset)."""
        uid = request.match_info["uid"]
        q = request.query
        since = int(q.get("since_id", "0"))
        maxr = int(q.get("max_results", "10"))
        offset = int(q.get("pagination_token", "0"))
        exclude = set((q.get("exclude") or "").split(",")) - {""}
        posts = []
        for p in self.timelines.get(uid, []):
            types = {r["type"] for r in p["data"].get("referenced_tweets", [])}
            if int(p["data"]["id"]) <= since or ("replies" in exclude and "replied_to" in types) or (
                    "retweets" in exclude and "retweeted" in types):
                continue
            posts.append(p)
        posts.sort(key=lambda p: int(p["data"]["id"]), reverse=True)
        page = posts[offset: offset + maxr]
        self.ev(f"timeline uid={uid} since_id={since} token={offset or '-'} -> {len(page)}/{len(posts)}")
        headers: dict[str, str] = {}
        if self.rate_limit_limit is not None:
            headers = {"x-rate-limit-limit": str(self.rate_limit_limit),
                       "x-rate-limit-remaining": str(max(self.rate_limit_limit - 5, 10)),
                       "x-rate-limit-reset": str(int(time.time()) + 900)}
        if not page:
            return web.json_response({"meta": {"result_count": 0}}, headers=headers)
        meta: dict[str, Any] = {"oldest_id": pid_of(page[-1]), "result_count": len(page)}
        if offset == 0:
            meta["newest_id"] = pid_of(page[0])
        if offset + maxr < len(posts):
            meta["next_token"] = str(offset + maxr)
        users = {u["id"]: u for p in page for u in p["includes"]["users"]}
        return web.json_response({"data": [p["data"] for p in page], "includes": {"users": list(users.values())},
                                  "meta": meta}, headers=headers)

    async def stream(self, request: web.Request) -> web.StreamResponse:
        if "tweet.fields" not in request.query:  # the access-check probe
            self.ev("stream probe")
            if self.no_stream:
                return web.json_response(FORBIDDEN, status=403)
            return web.Response(status=200, text="")
        if self.no_stream:
            self.ev("stream connect -> 403")
            return web.json_response(FORBIDDEN, status=403)
        self.stream_conns.append(time.monotonic())
        n = len(self.stream_conns)
        self.ev(f"stream connect n={n}")
        script = self.stream_scripts[min(n, len(self.stream_scripts)) - 1]
        try:
            return await script(self, request)
        except _DROPPED:
            self.ev(f"stream conn {n} dropped by the client")
            raise


class FakeNtfy:
    def __init__(self) -> None:
        self.messages: list[dict[str, Any]] = []

    def app(self) -> web.Application:
        app = web.Application()
        app.router.add_post("/", self.publish)
        return app

    async def publish(self, request: web.Request) -> web.Response:
        body = await request.json()
        body["_auth"] = request.headers.get("Authorization")
        self.messages.append(body)
        return web.json_response({"id": "x", "event": "message"})


async def serve(app: web.Application, port: int = 0) -> tuple[web.AppRunner, int]:
    runner = web.AppRunner(app, handler_cancellation=True, shutdown_timeout=1.0)
    await runner.setup()
    await web.TCPSite(runner, "127.0.0.1", port).start()
    return runner, int(runner.addresses[0][1])


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


@pytest.fixture
async def fake_x():
    fx = FakeX()
    runner, port = await serve(fx.app())
    fx.base_url = f"http://127.0.0.1:{port}"  # type: ignore[attr-defined]
    try:
        yield fx
    finally:
        fx.release.set()
        await runner.cleanup()


# --------------------------------------------------------------------------- in-process helpers


@pytest.fixture(autouse=True)
def clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in SECRET_ENV_VARS:
        monkeypatch.delenv(name, raising=False)


@pytest.fixture(autouse=True)
def restore_logging():
    """Service.run() calls setup_logging(), which adds root handlers; remove them afterwards."""
    root = logging.getLogger()
    before = list(root.handlers)
    level = root.level
    yield
    for h in list(root.handlers):
        if h not in before and any(isinstance(f, RedactingFilter) for f in h.filters):
            root.removeHandler(h)
    root.setLevel(level)


@pytest.fixture
def capture(monkeypatch: pytest.MonkeyPatch) -> CaptureNotifier:
    notifier = CaptureNotifier("capture")

    def fake_build(cfg: AppConfig, session: aiohttp.ClientSession) -> AlertDispatcher:
        return AlertDispatcher(cfg.settings.alerts, [notifier])

    monkeypatch.setattr(app_mod, "build_dispatcher", fake_build)
    return notifier


def make_cfg(tmp_path: Path, base_url: str, *, token: str | None = TOKEN) -> AppConfig:
    s = Settings()
    s.x_api.base_url = base_url
    s.x_api.stream.heartbeat_timeout_seconds = 5.0
    s.alerts.quiet_hours.enabled = False
    s.ops.daily_summary.enabled = False
    s.ops.db_path = str(tmp_path / "ceoscan.db")
    s.ops.health_host = "127.0.0.1"
    s.ops.health_port = 0
    s.scoring.enabled = False  # fast rules only (no Claude in tests)
    wl = Watchlist(
        accounts=(
            Account("elonmusk", ("TSLA",), user_id=ELON, always_alert=True, expect_name="Elon Musk"),
            Account("AMD", ("AMD",), kind="company", user_id=AMD, include_replies=False),
        ),
        keyword_rules=(),
        my_tickers=("TSLA", "AMD"),
        company_names={"TSLA": ("Tesla",), "AMD": ("AMD",)},
    )
    return AppConfig(settings=s, watchlist=wl, secrets=Secrets(x_bearer_token=token),
                     settings_path=tmp_path / "settings.yaml", watchlist_path=tmp_path / "watchlist.yaml")


async def wait_until(cond: Callable[[], bool], task: asyncio.Task[Any] | None = None, timeout: float = 10.0) -> None:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while not cond():
        if task is not None and task.done():
            raise AssertionError(f"service exited early with {task.result()!r}")
        if loop.time() > deadline:
            raise AssertionError("timed out waiting for condition")
        await asyncio.sleep(0.02)


def titles(n: CaptureNotifier, kind: str = "system") -> list[str]:
    return [m.title for m in n.sent if m.kind == kind]


async def stop(svc: Service, task: asyncio.Task[int]) -> int:
    svc.request_stop()
    return await asyncio.wait_for(task, timeout=20)


# --------------------------------------------------------------------------- in-process: startup / config problems


async def test_startup_alert_waits_for_ingest_and_config_problem_alerts_once(
    tmp_path: Path, fake_x: FakeX, capture: CaptureNotifier
) -> None:
    """SEC-8: a rejected rule sends no 'ceoscan started', one 'stopped: configuration problem'
    alert, exits 2 - and a restart loop with the same problem does not alert again."""
    fake_x.reject_rules = True
    cfg = make_cfg(tmp_path, fake_x.base_url)  # type: ignore[attr-defined]
    for _ in range(2):  # the second run is the restart-loop case
        svc = Service(cfg, out=lambda _l: None, install_signal_handlers=False)
        assert await asyncio.wait_for(svc.run(), timeout=20) == EXIT_CONFIG
    assert not any(t.startswith("ceoscan started") for t in titles(capture))
    problems = [m for m in capture.sent if m.title.startswith(CONFIG_ALERT_TITLE)]
    assert len(problems) == 1
    assert "UnprocessableEntity" in problems[0].body or "operator" in problems[0].body
    assert problems[0].priority == "high"
    assert not fake_x.stream_conns
    store = Store(cfg.settings.ops.db_path)
    try:
        assert json.loads(store.kv_get(KV_CONFIG_ALERT) or "{}").get("hash")
    finally:
        store.close()

    # a different problem is a new alert
    cfg2 = make_cfg(tmp_path, fake_x.base_url)  # type: ignore[attr-defined]
    cfg2.settings.x_api.mode = "stream"
    fake_x.reject_rules, fake_x.no_stream = False, True
    assert await asyncio.wait_for(Service(cfg2, out=lambda _l: None, install_signal_handlers=False).run(), 20) == 2
    assert len([t for t in titles(capture) if t.startswith(CONFIG_ALERT_TITLE)]) == 2


async def test_config_problem_wait_mode_stays_up_with_health_config_error(
    tmp_path: Path, fake_x: FakeX, capture: CaptureNotifier
) -> None:
    cfg = make_cfg(tmp_path, fake_x.base_url, token=None)  # type: ignore[attr-defined]
    cfg.settings.ops.config_error_action = "wait"
    lines: list[str] = []
    svc = Service(cfg, out=lines.append, install_signal_handlers=False)
    task = asyncio.create_task(svc.run())
    await wait_until(lambda: svc.status.state == "config_error" and bool(capture.sent), task)
    await asyncio.sleep(0.3)
    assert not task.done()  # idle, not exiting (no Docker restart loop)
    assert svc.health is not None
    async with aiohttp.ClientSession() as s, s.get(f"http://127.0.0.1:{svc.health.port}/health") as resp:
        health = await resp.json()
    assert health["stream"]["state"] == "config_error"
    assert "X_BEARER_TOKEN" in (health["stream"]["last_error"] or "")
    assert health["status"] in ("degraded", "down")
    [alert] = capture.sent
    assert alert.title.startswith(CONFIG_ALERT_TITLE) and "X_BEARER_TOKEN" in alert.body
    assert "idle" in alert.body
    assert any("config_error" in line for line in lines)
    assert await stop(svc, task) == EXIT_CONFIG
    assert not (tmp_path / "ceoscan.db").exists()  # nothing written for a pre-start problem
    assert not fake_x.log  # X was never contacted


async def test_missing_token_restart_loop_alerts_once_when_the_database_exists(
    tmp_path: Path, fake_x: FakeX, capture: CaptureNotifier
) -> None:
    cfg = make_cfg(tmp_path, fake_x.base_url, token=None)  # type: ignore[attr-defined]
    Store(cfg.settings.ops.db_path).close()  # an existing deployment
    for _ in range(3):  # e.g. a container restart policy re-running it with ops.config_error_action=exit
        assert await Service(cfg, out=lambda _l: None, install_signal_handlers=False).run() == EXIT_CONFIG
    assert len(capture.sent) == 1 and capture.sent[0].title.startswith(CONFIG_ALERT_TITLE)


# --------------------------------------------------------------------------- in-process: restart catch-up


async def test_stream_restart_catches_up_with_age_floor_and_last_alive_baseline(
    tmp_path: Path, fake_x: FakeX, capture: CaptureNotifier
) -> None:
    """ING-5 / SEC-5 / ING-3: after a restart in stream mode, posts made while ceoscan was down are
    fetched once after the first connect; never older than recovery.max_age_seconds; accounts
    without a since_id start from the persisted last-alive time."""
    cfg = make_cfg(tmp_path, fake_x.base_url)  # type: ignore[attr-defined]
    cfg.settings.x_api.recovery.min_gap_seconds = 3600  # the startup catch-up must not depend on it
    now = now_utc()
    last_alive = now - timedelta(minutes=10)
    store = Store(cfg.settings.ops.db_path)
    store.kv_set(KV_LAST_ALIVE, last_alive.isoformat())
    store.set_since_id(ELON, str(datetime_to_snowflake(now - timedelta(days=3))))  # stopped for days
    store.close()
    elon_recent = make_post(ELON, "$TSLA guidance raised: record 500,000 deliveries", cashtags=("TSLA",),
                            ms=ms_ago(300))
    elon_stale = make_post(ELON, "$TSLA earnings beat, revenue up 40%", cashtags=("TSLA",), ms=ms_ago(7200))
    amd_recent = make_post(AMD, "$AMD announces a $6B buyback", cashtags=("AMD",), ms=ms_ago(480))
    amd_before = make_post(AMD, "$AMD raises guidance", cashtags=("AMD",), ms=ms_ago(900))  # before last-alive
    fake_x.timelines[ELON] = [elon_stale, elon_recent]
    fake_x.timelines[AMD] = [amd_before, amd_recent]

    svc = Service(cfg, out=lambda _l: None, install_signal_handlers=False)
    task = asyncio.create_task(svc.run())
    await wait_until(lambda: svc.store is not None and svc.store.post_exists(pid_of(elon_recent))
                     and svc.store.post_exists(pid_of(amd_recent)), task)
    await wait_until(lambda: any(t.startswith("ceoscan started (stream") for t in titles(capture)), task)
    assert await stop(svc, task) == EXIT_OK

    elon_req = fake_x.timeline_requests(ELON)[0]
    floor = datetime_to_snowflake(now - timedelta(seconds=cfg.settings.x_api.recovery.max_age_seconds))
    assert int(elon_req["since_id"]) >= floor - (5_000 << 22)  # floored, not 3 days back
    assert fake_x.timeline_requests(AMD)[0]["since_id"] == str(datetime_to_snowflake(last_alive))
    first_timeline = next(i for i, r in enumerate(fake_x.log) if r["path"].startswith("/2/users/"))
    first_connect = next(i for i, r in enumerate(fake_x.log) if r["path"] == "/2/tweets/search/stream" and r["query"])
    assert first_timeline > first_connect  # caught up once the stream was connected
    store = Store(cfg.settings.ops.db_path)
    try:
        assert store.get_post_row(pid_of(elon_recent))["source"] == "recovery"
        assert not store.post_exists(pid_of(elon_stale))  # older than max_age: never fetched
        assert not store.post_exists(pid_of(amd_before))  # before the last-alive time
        assert store.get_since_id(ELON) == pid_of(elon_recent)
        alive = store.kv_get(KV_LAST_ALIVE)
        assert alive is not None and alive > last_alive.isoformat()  # refreshed at shutdown
        assert store.kv_get("recovery_pending") == "{}"
    finally:
        store.close()
    elon_alerts = [m for m in capture.sent if m.kind == "post" and pid_of(elon_recent) in (m.url or "")]
    assert len(elon_alerts) == 1


# --------------------------------------------------------------------------- in-process: warm-up, workers, events


class RejectingScorer:
    """A Claude scorer whose key was revoked: warm-up and every request are rejected."""

    def __init__(self, settings: Any) -> None:
        self._inner = OfflineScorer(settings)
        self.warmup_transient: bool | None = None
        self.last_rejection: str | None = None
        self.warmups = 0

    async def warmup(self) -> str | None:
        self.warmups += 1
        self.warmup_transient = False
        self.last_rejection = "HTTP 401 authentication_error: invalid x-api-key"
        return "Claude rejected the warm-up request (HTTP 401 authentication_error: invalid x-api-key)"

    async def score(self, *args: Any, **kwargs: Any) -> Any:
        return await self._inner.score(*args, **kwargs)

    async def aclose(self) -> None:
        return None


async def test_claude_warmup_rejection_warns_alerts_once_and_keeps_running(
    tmp_path: Path, fake_x: FakeX, capture: CaptureNotifier, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg = make_cfg(tmp_path, fake_x.base_url)  # type: ignore[attr-defined]
    scorer = RejectingScorer(cfg.settings.scoring)
    monkeypatch.setattr(Service, "_make_scorer", lambda self: scorer)
    monkeypatch.setattr(app_mod, "HOUSEKEEPING_INTERVAL_SECONDS", 0.05)
    lines: list[str] = []
    svc = Service(cfg, out=lines.append, install_signal_handlers=False)
    task = asyncio.create_task(svc.run())
    await wait_until(lambda: any("Claude scoring unavailable" in t for t in titles(capture)), task)
    await wait_until(lambda: svc.status.state == "connected", task)
    await asyncio.sleep(0.3)  # several housekeeping ticks see scorer.last_rejection: no repeat alert
    assert not task.done()
    assert scorer.warmups == 1
    assert len([t for t in titles(capture) if "Claude scoring unavailable" in t]) == 1
    assert any(line.startswith("WARNING: Claude rejected") for line in lines)
    assert await stop(svc, task) == EXIT_OK


async def test_pipeline_workers_and_stream_error_events(
    tmp_path: Path, fake_x: FakeX, capture: CaptureNotifier, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: list[int] = []
    real_run = app_mod.Pipeline.run

    async def run(self: Any, queue: asyncio.Queue, stop_event: asyncio.Event, workers: int = 4) -> None:
        seen.append(workers)
        await real_run(self, queue, stop_event, workers=workers)

    monkeypatch.setattr(app_mod.Pipeline, "run", run)
    fake_x.stream_scripts = [drop_then_end, keepalives]
    cfg = make_cfg(tmp_path, fake_x.base_url)  # type: ignore[attr-defined]
    cfg.settings.ops.pipeline_workers = 3
    svc = Service(cfg, out=lambda _l: None, install_signal_handlers=False)
    task = asyncio.create_task(svc.run())
    await wait_until(lambda: len(fake_x.stream_conns) >= 2 and svc.status.state == "connected", task)
    assert await stop(svc, task) == EXIT_OK
    assert seen == [3]
    store = Store(cfg.settings.ops.db_path)
    try:
        events = store.recent_events(now_utc() - timedelta(minutes=5))
    finally:
        store.close()
    errors = [e for e in events if e["kind"] == "stream_error"]
    assert errors and errors[0]["level"] == "error" and "closed" in errors[0]["detail"]
    assert TOKEN not in json.dumps(events)
    # started once, after the first connect
    assert [t for t in titles(capture) if t.startswith("ceoscan started")] == ["ceoscan started (stream, 2 accounts, est. 4.5s)"]


async def test_claude_desk_fires_for_a_strong_alert_without_delaying_it(
    tmp_path: Path, fake_x: FakeX, capture: CaptureNotifier, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The Claude analyst desk inside the real service: the alert is pushed first (with the brief line),
    then one fire reaches the scheduled task's endpoint, is recorded, and shows up in /health."""
    fires: list[dict[str, Any]] = []

    async def fire(request: web.Request) -> web.Response:
        fires.append({"headers": dict(request.headers), "json": await request.json()})
        return web.json_response({"type": "routine_fire", "claude_code_session_id": "session_01E2E",
                                  "claude_code_session_url": "https://claude.ai/code/session_01E2E"})

    desk_app = web.Application()
    desk_app.router.add_post("/v1/claude_code/routines/{rid}/fire", fire)
    runner, port = await serve(desk_app)
    desk_token = "sk-ant-oat01-e2e-desk-token-NOT-REAL-42"

    def build_desk(cfg: AppConfig, store: Store, session: aiohttp.ClientSession, dispatcher: Any) -> Any:
        # the real factory only accepts api.anthropic.com; point the same desk at the local fake
        url = f"http://127.0.0.1:{port}/v1/claude_code/routines/trig_01E2E/fire"
        return ClaudeDesk(cfg, store, session, dispatcher, fire_url=url, token=desk_token), None

    monkeypatch.setattr(app_mod, "build_desk", build_desk)
    cfg = make_cfg(tmp_path, fake_x.base_url)  # type: ignore[attr-defined]
    cfg.secrets = Secrets(x_bearer_token=TOKEN, claude_desk_token=desk_token,
                          claude_desk_fire_url="https://api.anthropic.com/v1/claude_code/routines/trig_01E2E/fire")
    d = cfg.settings.claude_desk
    d.enabled, d.include_rules_only, d.batch_window_seconds = True, True, 0.2  # scoring is offline in tests
    d.daily_review = False  # otherwise a run near 18:00 PT also fires the daily review (wall-clock flake)
    store = Store(cfg.settings.ops.db_path)
    store.kv_set(KV_LAST_ALIVE, (now_utc() - timedelta(minutes=10)).isoformat())
    store.close()
    post = make_post(ELON, "$TSLA guidance raised: record 500,000 deliveries", cashtags=("TSLA",), ms=ms_ago(120))
    fake_x.timelines[ELON] = [post]
    out: list[str] = []
    svc = Service(cfg, out=out.append, install_signal_handlers=False)
    task = asyncio.create_task(svc.run())
    try:
        await wait_until(lambda: len(fires) == 1, task)
        await wait_until(lambda: svc.health is not None
                         and svc.health.snapshot()["claude_desk"]["fires_today"] == 1, task)
        health = svc.health.snapshot()["claude_desk"]  # type: ignore[union-attr]
        assert await stop(svc, task) == EXIT_OK
    finally:
        await runner.cleanup()
    (alert,) = [m for m in capture.sent if m.kind == "post"]
    assert BRIEF_LINE in alert.body
    text = fires[0]["json"]["text"]
    assert "Kind: alert" in text and pid_of(post) in text and "$TSLA guidance raised" in text
    assert fires[0]["headers"]["Authorization"] == f"Bearer {desk_token}"
    assert health["configured"] is True and health["last_session_url"] == "https://claude.ai/code/session_01E2E"
    assert any(line.startswith("Claude analyst desk: on") for line in out)
    assert svc.desk is not None and not svc.desk.running  # stopped with the service
    store = Store(cfg.settings.ops.db_path)
    try:
        (row,) = store.desk_fires()
        assert row["status"] == "sent" and row["post_ids"] == [pid_of(post)]
    finally:
        store.close()
    assert desk_token not in json.dumps(health) and desk_token not in text


async def test_polling_repaces_to_the_real_rate_limit_and_tells_the_user(
    tmp_path: Path, fake_x: FakeX, capture: CaptureNotifier
) -> None:
    """REQ-1: X says 60 requests / 15 min (settings assume 10,000): new interval and latency are
    printed, put in the status and sent as a system alert."""
    fake_x.no_stream = True
    fake_x.rate_limit_limit = 60
    cfg = make_cfg(tmp_path, fake_x.base_url)  # type: ignore[attr-defined]
    lines: list[str] = []
    svc = Service(cfg, out=lines.append, install_signal_handlers=False)
    task = asyncio.create_task(svc.run())
    await wait_until(lambda: any("re-paced" in line for line in lines), task)
    await wait_until(lambda: any(t.startswith("ceoscan polling latency now") for t in titles(capture)), task)
    expected = 2 * 900 / (60 * 0.8)  # 37.5 s per account
    assert svc.status.mode == "poll"
    assert svc.status.poll_interval_seconds == pytest.approx(expected)
    assert svc.status.estimated_latency_seconds == pytest.approx(expected / 2 + 2)
    assert any(n.startswith("polling: every 37.5s") for n in svc.status.notes)
    assert len([n for n in svc.status.notes if n.startswith("polling:")]) == 1
    assert await stop(svc, task) == EXIT_OK
    assert any(t.startswith("ceoscan started (poll") for t in titles(capture))


# --------------------------------------------------------------------------- subprocess scenarios (CEOSCAN_E2E=1)


def write_config(tmp: Path, xport: int, nport: int, hport: int) -> tuple[Path, Path, Path]:
    settings, watchlist, db = tmp / "settings.yaml", tmp / "watchlist.yaml", tmp / "ceoscan.db"
    settings.write_text(f"""
x_api:
  mode: auto
  base_url: http://127.0.0.1:{xport}
  stream:
    heartbeat_timeout_seconds: 4
    connect_timeout_seconds: 15
    terminate_stale_connections: true
  recovery:
    enabled: true
    min_gap_seconds: 2
    max_results: 5
    max_pages: 5
  poll:
    min_interval_seconds: 1.0
scoring:
  enabled: false
alerts:
  push: ntfy
  ntfy:
    server: http://127.0.0.1:{nport}
  quiet_hours:
    enabled: false
ops:
  health_host: 127.0.0.1
  health_port: {hport}
  db_path: {db}
  log_level: INFO
  daily_summary:
    enabled: false
  config_error_action: exit
""")
    watchlist.write_text(f"""
my_tickers: [NVDA]
accounts:
  - {{handle: elonmusk, kind: person, expect_name: Elon Musk, tickers: [TSLA], always_alert: true,
      include_replies: true, user_id: "{ELON}"}}
  - {{handle: AMD, kind: company, tickers: [AMD], include_replies: false, user_id: "{AMD}"}}
  - {{handle: saylor, kind: person, tickers: [MSTR], always_alert: true, user_id: "{SAYLOR}"}}
  - {{handle: nvidia, kind: company, tickers: [NVDA], always_alert: true, include_replies: false,
      user_id: "{NVIDIA}"}}
  - {{handle: notverified, kind: person, tickers: [AAPL]}}
keyword_rules:
  - {{name: big, tickers: [NVDA, TSLA], min_followers: 1000000}}
""")
    return settings, watchlist, db


class ServiceProcess:
    """The real service as a subprocess, its output captured line by line."""

    def __init__(self, settings: Path, watchlist: Path, cwd: Path) -> None:
        self.cmd = [sys.executable, "-m", "ceoscan", "--settings", str(settings), "--watchlist", str(watchlist),
                    "--env", str(cwd / "no-such.env"), "run"]
        self.cwd = cwd
        self.lines: list[str] = []
        self.proc: asyncio.subprocess.Process | None = None
        self._pumps: list[asyncio.Task[None]] = []

    async def start(self) -> None:
        env = {k: v for k, v in os.environ.items() if k.upper() not in SECRET_ENV_VARS}
        env.update({"X_BEARER_TOKEN": TOKEN, "NTFY_TOPIC": NTFY_TOPIC, "PYTHONPATH": str(ROOT / "src"),
                    "PYTHONUNBUFFERED": "1", "NO_PROXY": "127.0.0.1,localhost", "no_proxy": "127.0.0.1,localhost"})
        env.pop("CEOSCAN_CONFIG_ERROR_ACTION", None)
        env.pop("CEOSCAN_HEALTH_HOST", None)
        self.proc = await asyncio.create_subprocess_exec(*self.cmd, cwd=str(self.cwd), env=env,
                                                         stdout=asyncio.subprocess.PIPE,
                                                         stderr=asyncio.subprocess.PIPE)
        assert self.proc.stdout is not None and self.proc.stderr is not None
        self._pumps = [asyncio.create_task(self._pump(self.proc.stdout)),
                       asyncio.create_task(self._pump(self.proc.stderr))]

    async def _pump(self, stream: asyncio.StreamReader) -> None:
        while line := await stream.readline():
            self.lines.append(line.decode(errors="replace").rstrip())

    @property
    def text(self) -> str:
        return "\n".join(self.lines)

    async def sigterm(self, timeout: float = 20.0) -> tuple[int, float]:
        assert self.proc is not None
        t0 = time.monotonic()
        if self.proc.returncode is None:
            self.proc.send_signal(signal.SIGTERM)
        try:
            rc = await asyncio.wait_for(self.proc.wait(), timeout)
        except TimeoutError:
            self.proc.kill()
            rc = await self.proc.wait()
        await asyncio.gather(*self._pumps)
        return rc, time.monotonic() - t0

    async def kill(self) -> None:
        if self.proc is not None and self.proc.returncode is None:
            self.proc.kill()
            await self.proc.wait()


async def get_health(port: int) -> dict[str, Any] | None:
    try:
        async with aiohttp.ClientSession(trust_env=False) as s, s.get(
                f"http://127.0.0.1:{port}/health", timeout=aiohttp.ClientTimeout(total=2)) as r:
            return await r.json()
    except (aiohttp.ClientError, TimeoutError, ValueError):
        return None


async def wait_for(cond: Callable[[], bool], svc: ServiceProcess, timeout: float, what: str) -> None:
    deadline = time.monotonic() + timeout
    while not cond():
        assert svc.proc is not None
        if svc.proc.returncode is not None:
            raise AssertionError(f"service exited ({svc.proc.returncode}) while waiting for {what}\n{svc.text[-4000:]}")
        if time.monotonic() > deadline:
            raise AssertionError(f"timed out waiting for {what}\n{svc.text[-4000:]}")
        await asyncio.sleep(0.2)


def db_rows(db: Path, sql: str) -> list[dict[str, Any]]:
    con = sqlite3.connect(db)
    con.row_factory = sqlite3.Row
    try:
        return [dict(r) for r in con.execute(sql)]
    finally:
        con.close()


REQUIRED_IN_PUSH = ("@", "/10", "Why:", "Posted ", " PT", "https://x.com/")


def check_common(svc: ServiceProcess, ntfy: FakeNtfy, fx: FakeX, db: Path, rc: int, shutdown_s: float,
                 health: dict[str, Any] | None) -> list[dict[str, Any]]:
    from ceoscan.alerts.format import RULES_ONLY_MARKER

    text = svc.text
    assert rc == EXIT_OK, text[-4000:]
    assert shutdown_s < 20
    assert "Traceback" not in text, text[-6000:]
    assert TOKEN not in text and NTFY_TOPIC not in text  # no secrets in the logs
    assert all(r["auth"] == f"Bearer {TOKEN}" for r in fx.log)
    assert "ALERTS ONLY" in text
    assert health is not None and {"status", "mode", "stream", "last_post", "today"} <= set(health)
    assert health["status"] == "ok", health
    post_alerts = db_rows(db, "SELECT * FROM alerts WHERE kind='post' AND sent_ok=1")
    assert post_alerts
    assert all(a["total_latency_ms"] is not None for a in post_alerts)
    latency_lines = [line for line in svc.lines if "ALERT @" in line and "latency total=" in line]
    assert len(latency_lines) == len(post_alerts), "\n".join(latency_lines)
    pushes = [m for m in ntfy.messages if "/status/" in m.get("message", "")]
    assert len(pushes) == len(post_alerts)
    for m in pushes:
        assert m["topic"] == NTFY_TOPIC
        assert all(r in m["message"] for r in REQUIRED_IN_PUSH), m
        assert RULES_ONLY_MARKER in m["title"], m  # scoring disabled: every alert is labelled rules-only
    system = [m["title"] for m in ntfy.messages if "/status/" not in m.get("message", "")]
    assert len([t for t in system if t.startswith("ceoscan started")]) == 1
    assert db_rows(db, f"SELECT value FROM kv WHERE key='{KV_LAST_ALIVE}'")
    return post_alerts


@needs_e2e
async def test_e2e_stream_with_forced_reconnects_and_gap_recovery(tmp_path: Path) -> None:
    fx, ntfy = FakeX(), FakeNtfy()
    fx.keepalive_every = 1.0
    sent: dict[str, dict[str, Any]] = {}

    async def send(resp: web.StreamResponse, payload: dict[str, Any], record: bool = True) -> None:
        if record:
            sent[pid_of(payload)] = payload
            fx.timelines[payload["data"]["author_id"]].append(payload)  # the post is on the timeline too
        await resp.write(json.dumps(payload).encode() + b"\r\n")

    async def conn1_posts_then_tcp_drop(fx_: FakeX, request: web.Request) -> web.StreamResponse:
        resp = await open_stream(request)
        await resp.write(b"\r\n")
        await asyncio.sleep(0.3)
        a = make_post(ELON, "$TSLA Q3 earnings: revenue $25.2B, record 500,000 deliveries, guidance raised",
                      cashtags=("TSLA",), ms=ms_ago(1.5))
        await send(resp, a)
        await send(resp, make_post(AMD, "AMD and Microsoft sign a multi-year $8B partnership for MI450 GPUs",
                                   ms=ms_ago(1.5)))
        await send(resp, make_post(SAYLOR, "Good morning, have a great day", ms=ms_ago(1.0)))
        await send(resp, a, record=False)  # duplicate delivery: deduped
        for _ in range(2):
            await asyncio.sleep(0.5)
            await resp.write(b"\r\n")
        fx_.ev("conn1: dropping the TCP connection")
        assert request.transport is not None
        request.transport.close()
        return resp

    async def conn2_too_many_connections(fx_: FakeX, request: web.Request) -> web.StreamResponse:
        # posted while we are disconnected: only on the timelines
        m = make_post(ELON, "$TSLA announces acquisition of a battery maker for $3B, guidance raised",
                      cashtags=("TSLA",), ms=ms_ago(0.9))
        fx_.timelines[ELON].append(m)
        fx_.missed["elon_important_oldest"] = pid_of(m)
        for i in range(5):  # more than recovery.max_results (5): needs a second page
            fx_.timelines[ELON].append(make_post(ELON, f"haha {i}", ms=ms_ago(0.8) + i * 10))
        am = make_post(AMD, "$AMD board approves a $6B buyback and raises Q4 revenue guidance to $9.5B",
                       cashtags=("AMD",), ms=ms_ago(0.7))
        fx_.timelines[AMD].append(am)
        fx_.missed["amd_important"] = pid_of(am)
        ar = make_post(AMD, "@someone thanks!", ref={"type": "replied_to", "id": "1"}, ms=ms_ago(0.65))
        fx_.timelines[AMD].append(ar)
        fx_.missed["amd_reply_excluded"] = pid_of(ar)
        nv = make_post(NVIDIA, "$NVDA signs a $20B partnership with a sovereign AI fund", cashtags=("NVDA",),
                       ms=ms_ago(0.6))
        fx_.timelines[NVIDIA].append(nv)
        fx_.missed["nvidia_first_post"] = pid_of(nv)  # no stored since_id: needs the baseline
        return web.json_response(
            {"title": "ConnectionException", "connection_issue": "TooManyConnections",
             "detail": "This stream is currently at the maximum allowed connection limit.",
             "type": "https://api.twitter.com/2/problems/streaming-connection"},
            status=429,
            headers={"x-rate-limit-limit": "50", "x-rate-limit-remaining": "45",
                     "x-rate-limit-reset": str(int(time.time()) + 900)},
        )

    async def conn3_one_keepalive_then_silence(fx_: FakeX, request: web.Request) -> web.StreamResponse:
        resp = await open_stream(request)
        await resp.write(b"\r\n")
        fx_.ev("conn3: silent")
        await fx_.release.wait()
        return resp

    async def conn4_steady_with_post(fx_: FakeX, request: web.Request) -> web.StreamResponse:
        resp = await open_stream(request)
        with contextlib.suppress(*_DROPPED):
            await resp.write(b"\r\n")
            await asyncio.sleep(0.5)
            p = make_post(SAYLOR, "We bought 10,000 BTC for $1.1 billion. $MSTR now holds 700,000 bitcoin.",
                          cashtags=("MSTR",), ms=ms_ago(1.2))
            fx_.missed["live_after_recovery"] = pid_of(p)
            await send(resp, p)
            while not fx_.release.is_set():
                await resp.write(b"\r\n")
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(fx_.release.wait(), fx_.keepalive_every)
        return resp

    fx.stream_scripts = [conn1_posts_then_tcp_drop, conn2_too_many_connections, conn3_one_keepalive_then_silence,
                         conn4_steady_with_post]
    xrun, xport = await serve(fx.app())
    nrun, nport = await serve(ntfy.app())
    hport = free_port()
    settings, watchlist, db = write_config(tmp_path, xport, nport, hport)
    svc = ServiceProcess(settings, watchlist, tmp_path)
    try:
        await svc.start()
        await wait_for(lambda: len(fx.stream_conns) >= 4, svc, 30, "4 stream connections")
        missed_ids = [v for k, v in fx.missed.items() if k != "amd_reply_excluded"]
        await wait_for(lambda: db.exists() and all(
            db_rows(db, f"SELECT 1 FROM posts WHERE id='{i}'") for i in missed_ids), svc, 15, "recovered posts")

        def alerts_done() -> bool:
            live = fx.missed.get("live_after_recovery")
            pushes = [m for m in ntfy.messages if "/status/" in m.get("message", "")]
            return live is not None and any(live in m.get("click", "") for m in pushes) and len(pushes) >= len(
                db_rows(db, "SELECT 1 FROM alerts WHERE kind='post'"))

        await wait_for(alerts_done, svc, 10, "alerts")
        await asyncio.sleep(1.0)
        health = await get_health(hport)
        rc, shutdown_s = await svc.sigterm()
    finally:
        fx.release.set()
        await svc.kill()
        await xrun.cleanup()
        await nrun.cleanup()

    check_common(svc, ntfy, fx, db, rc, shutdown_s, health)
    # 3 successful connections (the TooManyConnections attempt is not one): 2 reconnects
    assert health is not None and health["mode"] == "stream" and health["stream"]["reconnects"] >= 2
    # rules: validated with a dry run first; our stale rule deleted, the foreign one kept
    assert any("dry_run=True" in e for e in fx.events)
    assert any("rules delete dry_run=False ids=['111']" in e for e in fx.events)
    assert any(r["id"] == "222" for r in fx.rules)
    # TooManyConnections: our stale connection terminated, retried within seconds (connects left)
    assert any("DELETE /2/connections/filtered_stream" in e for e in fx.events)
    assert fx.stream_conns[2] - fx.stream_conns[1] < 10
    # ING-4: a fresh connection that goes silent is dropped ~heartbeat_timeout (4 s) after its last
    # byte, not connect_timeout + heartbeat (19 s)
    assert fx.stream_conns[3] - fx.stream_conns[2] < 7.5
    # stream posts stored once each (duplicate delivery deduped)
    stream_posts = db_rows(db, "SELECT id FROM posts WHERE source='stream'")
    assert {r["id"] for r in stream_posts} == set(sent)
    # gap recovery: paginated (6 @elonmusk posts > max_results 5), baseline for a never-seen account,
    # include_replies: false honoured
    by_id = {r["id"]: r for r in db_rows(db, "SELECT id, source FROM posts")}
    for key in ("elon_important_oldest", "amd_important", "nvidia_first_post"):
        assert by_id[fx.missed[key]]["source"] == "recovery", key
    assert fx.missed["amd_reply_excluded"] not in by_id
    assert any(r.get("pagination_token") for r in fx.timeline_requests(ELON))
    alerted = {a["post_id"] for a in db_rows(db, "SELECT post_id FROM alerts WHERE kind='post'")}
    assert fx.missed["elon_important_oldest"] in alerted and fx.missed["nvidia_first_post"] in alerted
    newest_elon = max(int(p["data"]["id"]) for p in fx.timelines[ELON])
    assert db_rows(db, f"SELECT since_id FROM since_ids WHERE user_id='{ELON}'")[0]["since_id"] == str(newest_elon)
    # REQ-5: disconnects are in the events table for the daily summary
    assert db_rows(db, "SELECT 1 FROM events WHERE kind='stream_error' AND level='error'")
    # never a timeline request without since_id (no paid historical baseline)
    assert all("since_id" in r for r in fx.timeline_requests())


@needs_e2e
async def test_e2e_poll_fallback(tmp_path: Path) -> None:
    fx, ntfy = FakeX(), FakeNtfy()
    fx.no_stream = True
    xrun, xport = await serve(fx.app())
    nrun, nport = await serve(ntfy.app())
    hport = free_port()
    settings, watchlist, db = write_config(tmp_path, xport, nport, hport)
    svc = ServiceProcess(settings, watchlist, tmp_path)
    posts: dict[str, dict[str, Any]] = {}

    async def feed() -> None:
        plan = [(3, "elon", ELON, "$TSLA raises 2027 guidance: 3 million units, $150B revenue target", ("TSLA",)),
                (4, "amd_reply", AMD, "@fan thanks for the support!", ()),
                (5, "nvidia", NVIDIA, "$NVDA announces a $10B buyback and record revenue of $40B", ("NVDA",))]
        start = time.monotonic()
        for at, key, uid, text, tags in plan:
            await asyncio.sleep(max(0.0, start + at - time.monotonic()))
            ref = {"type": "replied_to", "id": "1"} if text.startswith("@") else None
            p = make_post(uid, text, cashtags=tags, ref=ref, ms=ms_ago(0))
            fx.timelines[uid].append(p)
            posts[key] = p

    try:
        await svc.start()
        await wait_for(lambda: "Timeline polling" in svc.text, svc, 15, "polling to start")
        await feed()
        await wait_for(lambda: len([m for m in ntfy.messages if "/status/" in m.get("message", "")]) >= 2,
                       svc, 15, "two post alerts")
        await asyncio.sleep(1.0)
        health = await get_health(hport)
        rc, shutdown_s = await svc.sigterm()
    finally:
        await svc.kill()
        await xrun.cleanup()
        await nrun.cleanup()

    check_common(svc, ntfy, fx, db, rc, shutdown_s, health)
    assert health is not None and health["mode"] == "poll" and health["stream"]["state"] == "polling"
    assert health["stream"]["poll_interval_seconds"] and health["stream"]["estimated_latency_seconds"]
    assert "falling back to timeline polling" in svc.text or "Timeline polling:" in svc.text
    assert "est. latency typical" in svc.text
    by_id = {r["id"]: r for r in db_rows(db, "SELECT id, source FROM posts")}
    assert by_id[pid_of(posts["elon"])]["source"] == "poll"
    assert by_id[pid_of(posts["nvidia"])]["source"] == "poll"
    assert pid_of(posts["amd_reply"]) not in by_id  # include_replies: false
    assert all(r.get("exclude") == "retweets,replies" for r in fx.timeline_requests(AMD))
    assert all("since_id" in r for r in fx.timeline_requests())
    assert not fx.stream_conns  # no stream connection on a plan without the filtered stream
    system = [m["title"] for m in ntfy.messages if "/status/" not in m.get("message", "")]
    assert any(t.startswith("ceoscan started (poll") for t in system)
