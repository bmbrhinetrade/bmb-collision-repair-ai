"""Tests for the CLI (ceoscan.__main__) and the service wiring (ceoscan.app).

Nothing here touches the real network: offline commands run with outbound sockets blocked, and
the X API is played by a local aiohttp server on 127.0.0.1 (``FakeX``). Alert channels are
replaced with CaptureNotifier.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import logging
import os
import shutil
import signal
import socket
import subprocess
from collections.abc import Callable
from datetime import timedelta
from pathlib import Path
from typing import Any

import aiohttp
import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer

import ceoscan.__main__ as cli
import ceoscan.app as app_mod
from ceoscan.alerts.dispatcher import AlertDispatcher
from ceoscan.alerts.notifiers import CaptureNotifier
from ceoscan.app import ALERTS_ONLY_BANNER, EXIT_CONFIG, EXIT_OK, Service
from ceoscan.config import SECRET_ENV_VARS, AppConfig, Secrets, Settings, load_app_config, load_watchlist
from ceoscan.logging_setup import RedactingFilter
from ceoscan.models import Account, KeywordRule, Watchlist
from ceoscan.store import Store
from ceoscan.timeutil import datetime_to_snowflake, now_utc

ROOT = Path(__file__).resolve().parents[1]
SETTINGS = ROOT / "config" / "settings.yaml"
WATCHLIST = ROOT / "config" / "watchlist.yaml"
SAMPLES = "samples/sample_posts.jsonl"
TOKEN = "test-bearer-token-123456"
ELON_ID = "44196397"

OTHER_ENV_VARS = (
    "TWILIO_FROM",
    "ALERT_SMS_TO",
    "SMTP_HOST",
    "SMTP_PORT",
    "SMTP_USERNAME",
    "ALERT_EMAIL_FROM",
    "ALERT_EMAIL_TO",
)

NOT_ENROLLED = {
    "client_id": "123",
    "detail": "When authenticating requests to the Twitter API v2 endpoints, you must use keys and tokens "
    "from a Twitter developer App that is attached to a Project.",
    "registration_url": "https://developer.x.com/en/docs/projects/overview",
    "required_enrollment": "Appropriate Level of API Access",
    "reason": "client-not-enrolled",
    "title": "Client Forbidden",
    "type": "https://api.twitter.com/2/problems/client-forbidden",
}


# --------------------------------------------------------------------------- fixtures


@pytest.fixture(autouse=True)
def clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """No secrets from the developer's shell may leak into (or out of) these tests."""
    for name in (*SECRET_ENV_VARS, *OTHER_ENV_VARS):
        monkeypatch.delenv(name, raising=False)


@pytest.fixture(autouse=True)
def restore_logging():
    """setup_logging() replaces the root handlers; drop the ones it added after each test."""
    root = logging.getLogger()
    before = list(root.handlers)
    level = root.level
    yield
    for h in list(root.handlers):
        if h not in before and any(isinstance(f, RedactingFilter) for f in h.filters):
            root.removeHandler(h)
    root.setLevel(level)


@pytest.fixture
def no_network(monkeypatch: pytest.MonkeyPatch) -> None:
    """Fail loudly if anything tries to open an internet socket."""
    real_connect = socket.socket.connect
    real_connect_ex = socket.socket.connect_ex

    def guard(self: socket.socket, address: Any, real: Callable[..., Any]) -> Any:
        if self.family in (socket.AF_INET, socket.AF_INET6):
            raise AssertionError(f"network access attempted: {address!r}")
        return real(self, address)

    monkeypatch.setattr(socket.socket, "connect", lambda self, a: guard(self, a, real_connect))
    monkeypatch.setattr(socket.socket, "connect_ex", lambda self, a: guard(self, a, real_connect_ex))


@pytest.fixture
def missing_env(tmp_path: Path) -> str:
    """A --env path that does not exist, so a developer's real .env is never loaded."""
    return str(tmp_path / "no-such.env")


def global_flags(env: str) -> list[str]:
    return ["--settings", str(SETTINGS), "--watchlist", str(WATCHLIST), "--env", env]


# --------------------------------------------------------------------------- argparse


@pytest.mark.parametrize(
    ("argv", "expected"),
    [
        (["run"], {"command": "run"}),
        (["verify"], {"command": "verify", "write": False, "accept_warnings": False}),
        (["verify", "--write"], {"command": "verify", "write": True, "accept_warnings": False}),
        (["verify", "--write", "--accept-warnings"], {"command": "verify", "write": True, "accept_warnings": True}),
        (["rules"], {"command": "rules", "apply": False}),
        (["rules", "--apply"], {"command": "rules", "apply": True}),
        (["check-access"], {"command": "check-access"}),
        (["cost"], {"command": "cost"}),
        (
            ["replay", "posts.jsonl"],
            {"command": "replay", "file": "posts.jsonl", "claude": False, "send": False, "keep_times": False,
             "db": None},
        ),
        (
            ["replay", "posts.jsonl", "--claude", "--send", "--keep-times", "--db", "out.db"],
            {"command": "replay", "file": "posts.jsonl", "claude": True, "send": True, "keep_times": True,
             "db": "out.db"},
        ),
        (["test-alert"], {"command": "test-alert", "message": None}),
        (["test-alert", "--message", "hello phone"], {"command": "test-alert", "message": "hello phone"}),
        (["test-desk"], {"command": "test-desk", "message": None}),
        (["test-desk", "--message", "hello desk"], {"command": "test-desk", "message": "hello desk"}),
        (["test-duet"], {"command": "test-duet", "message": None}),
        (["test-duet", "--message", "hello duet"], {"command": "test-duet", "message": "hello duet"}),
    ],
)
def test_parse_every_command(argv: list[str], expected: dict[str, Any]) -> None:
    ns = cli.build_parser().parse_args(argv)
    for key, value in expected.items():
        assert getattr(ns, key) == value, key
    # global defaults
    assert ns.settings == "config/settings.yaml"
    assert ns.watchlist == "config/watchlist.yaml"
    assert ns.env == ".env"


def test_parse_global_flags_before_command() -> None:
    ns = cli.build_parser().parse_args(["--settings", "s.yaml", "--watchlist", "w.yaml", "--env", "x.env", "cost"])
    assert (ns.settings, ns.watchlist, ns.env, ns.command) == ("s.yaml", "w.yaml", "x.env", "cost")


def test_every_subcommand_has_a_handler() -> None:
    parser = cli.build_parser()
    sub = next(a for a in parser._actions if isinstance(a, argparse._SubParsersAction))
    assert set(sub.choices) == set(cli.COMMANDS)


def _docker_entrypoint() -> list[str]:
    lines = (ROOT / "Dockerfile").read_text(encoding="utf-8").splitlines()
    (line,) = [ln for ln in lines if ln.startswith("ENTRYPOINT ")]
    return json.loads(line[len("ENTRYPOINT "):])


def test_docker_entrypoint_routes_every_cli_command_to_ceoscan() -> None:
    """`docker compose run --rm ceoscan <command>` must work for every command (the docs say so; DUET_SETUP
    step A6 uses test-duet). A command missing from the ENTRYPOINT's case list is exec'd as a program
    instead: "exec: test-duet: not found", exit 127."""
    shell, flag, script, argv0 = _docker_entrypoint()
    assert (shell, flag, argv0) == ("/bin/sh", "-c", "ceoscan")
    case = script[script.index('case "$1" in ') + len('case "$1" in '):]
    patterns = case[: case.index(")")].split("|")
    assert set(cli.COMMANDS) <= set(patterns), sorted(set(cli.COMMANDS) - set(patterns))
    sh = shutil.which("sh")
    if sh is None:  # pragma: no cover - every Linux / macOS CI has one
        pytest.skip("no POSIX shell")
    # the same routing with echo instead of exec: which branch runs for each first argument?
    probe = script.replace('exec python -m ceoscan "$@"', 'echo "ceoscan $*"').replace('exec "$@"', 'echo "exec $*"')
    assert probe != script and probe.count("echo") == 2

    def route(*args: str) -> str:
        done = subprocess.run([sh, "-c", probe, argv0, *args], capture_output=True, text=True, timeout=10, check=False)
        assert done.returncode == 0, done.stderr
        return done.stdout.strip()

    for command in cli.COMMANDS:
        assert route(command, "--help") == f"ceoscan {command} --help", command
    assert route() == "ceoscan run"  # no argument: the service
    assert route("--version") == "ceoscan --version"
    assert route("python", "-m", "ceoscan", "run") == "exec python -m ceoscan run"  # the default CMD runs as-is


@pytest.mark.parametrize("argv", [["replay"], ["bogus"], ["rules", "--write"], ["verify", "--apply"]])
def test_parse_errors_exit_2(argv: list[str]) -> None:
    with pytest.raises(SystemExit) as exc:
        cli.build_parser().parse_args(argv)
    assert exc.value.code == 2


def test_no_command_prints_help_and_returns_2(capsys: pytest.CaptureFixture[str]) -> None:
    assert cli.main([]) == 2
    assert "usage:" in capsys.readouterr().err


# --------------------------------------------------------------------------- offline commands


def test_cost_runs_offline_and_prints_a_total(
    no_network: None, missing_env: str, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.chdir(ROOT)  # default --settings / --watchlist paths are relative
    assert cli.main(["--env", missing_env, "cost"]) == 0
    out = capsys.readouterr().out
    assert "Estimated monthly cost" in out
    total = next(line for line in out.splitlines() if line.strip().startswith("Total:"))
    assert "$" in total and "/ month" in total
    assert "@elonmusk" in out


def test_replay_samples_runs_offline(
    no_network: None,
    missing_env: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.chdir(ROOT)
    # quiet hours off, so the result does not depend on the time of day the tests run at
    settings = tmp_path / "settings.yaml"
    text = SETTINGS.read_text(encoding="utf-8")
    settings.write_text(
        text.replace("  quiet_hours:\n    enabled: true", "  quiet_hours:\n    enabled: false"), encoding="utf-8"
    )
    assert "enabled: false" in settings.read_text(encoding="utf-8")
    assert cli.main(["--settings", str(settings), "--env", missing_env, "replay", SAMPLES]) == 0
    out = capsys.readouterr().out
    assert "Replay summary:" in out
    assert "X API was not contacted" in out
    assert "duplicate" in out  # the sample file contains a duplicate id
    assert "[REPLAY]" in out  # console notifier output is marked as a replay


def test_replay_keep_times_with_db_file(
    no_network: None, missing_env: str, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    db = tmp_path / "replay.db"
    code = cli.main([*global_flags(missing_env), "replay", str(ROOT / SAMPLES), "--keep-times", "--db", str(db)])
    assert code == 0
    assert "Replay summary:" in capsys.readouterr().out
    store = Store(db)
    try:
        assert store.stats_since(now_utc() - timedelta(days=1))["posts"] > 0
    finally:
        store.close()


def test_replay_missing_file_exits_2(missing_env: str, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert cli.main([*global_flags(missing_env), "replay", str(tmp_path / "nope.jsonl")]) == 2
    assert "not found" in capsys.readouterr().err


def test_bad_watchlist_path_exits_2(missing_env: str, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    code = cli.main(["--settings", str(SETTINGS), "--watchlist", str(tmp_path / "none.yaml"), "--env", missing_env,
                     "cost"])
    assert code == 2
    assert "could not load the configuration" in capsys.readouterr().err


# --------------------------------------------------------------------------- X_BEARER_TOKEN missing -> exit 2


@pytest.mark.parametrize("command", [["run"], ["verify"], ["verify", "--write"], ["check-access"], ["rules", "--apply"],
                                     ["rules"]])
def test_x_commands_without_token_exit_2(
    command: list[str],
    no_network: None,
    missing_env: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.chdir(tmp_path)  # nothing may be created in the project (e.g. data/ceoscan.db)
    assert cli.main([*global_flags(missing_env), *command]) == 2
    captured = capsys.readouterr()
    assert "X_BEARER_TOKEN" in captured.out + captured.err
    if command != ["run"]:  # `run` opens its database first, to dedupe the configuration-problem alert
        assert not (tmp_path / "data").exists()


def test_run_without_verified_accounts_exits_2(
    no_network: None,
    missing_env: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("X_BEARER_TOKEN", TOKEN)
    # the shipped watchlist now carries verified user_ids: use a copy without them
    unverified = tmp_path / "watchlist.yaml"
    unverified.write_text(
        "".join(line for line in WATCHLIST.read_text(encoding="utf-8").splitlines(keepends=True)
                if not line.strip().startswith("user_id:")),
        encoding="utf-8",
    )
    assert not any(a.user_id for a in load_watchlist(unverified).accounts)
    flags = ["--settings", str(SETTINGS), "--watchlist", str(unverified), "--env", missing_env]
    assert cli.main([*flags, "run"]) == 2
    out = capsys.readouterr().out
    assert "verify --write" in out
    assert ALERTS_ONLY_BANNER in out
    assert TOKEN not in out


# --------------------------------------------------------------------------- test-alert


def _patch_dispatcher(monkeypatch: pytest.MonkeyPatch, module: Any, **channels: Any) -> None:
    def fake_build(cfg: AppConfig, session: aiohttp.ClientSession) -> AlertDispatcher:
        return AlertDispatcher(cfg.settings.alerts, channels.get("push", []), sms=channels.get("sms"),
                               email=channels.get("email"))

    monkeypatch.setattr(module, "build_dispatcher", fake_build)


def test_test_alert_sends_to_every_channel(
    no_network: None, missing_env: str, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    push, sms, email = CaptureNotifier("ntfy"), CaptureNotifier("sms"), CaptureNotifier("email")
    _patch_dispatcher(monkeypatch, cli, push=[push], sms=sms, email=email)
    assert cli.main([*global_flags(missing_env), "test-alert", "--message", "hello phone"]) == 0
    for n in (push, sms, email):  # SMS/email are included even though a test alert has no score
        assert len(n.sent) == 1
        assert n.sent[0].kind == "system"
        assert n.sent[0].body == "hello phone"
    out = capsys.readouterr().out
    assert "ntfy" in out and "sms" in out and "email" in out and "OK" in out


def test_test_alert_reports_failed_channel(
    no_network: None, missing_env: str, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _patch_dispatcher(monkeypatch, cli, push=[CaptureNotifier("ntfy"), CaptureNotifier("pushover", ok=False)])
    assert cli.main([*global_flags(missing_env), "test-alert"]) == 1
    out = capsys.readouterr().out
    assert "FAILED" in out and "simulated failure" in out


def test_test_alert_without_channels_exits_2(
    no_network: None, missing_env: str, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _patch_dispatcher(monkeypatch, cli)
    assert cli.main([*global_flags(missing_env), "test-alert"]) == 2
    assert "no alert channel" in capsys.readouterr().err


# --------------------------------------------------------------------------- fake X API server


def fresh_id(offset_ms: int = 0) -> str:
    return str(datetime_to_snowflake(now_utc() - timedelta(seconds=2)) + (offset_ms << 22))


def post_obj(pid: str, text: str, uid: str = ELON_ID) -> dict[str, Any]:
    return {"id": pid, "text": text, "author_id": uid, "edit_history_tweet_ids": [pid], "lang": "en"}


def stream_payload(pid: str, text: str) -> dict[str, Any]:
    return {
        "data": post_obj(pid, text),
        "includes": {"users": [{"id": ELON_ID, "username": "elonmusk", "name": "Elon Musk"}]},
        "matching_rules": [{"id": "1", "tag": "ceoscan:acct:r1t0:1"}],
    }


def timeline_response(pid: str, text: str) -> dict[str, Any]:
    return {
        "data": [post_obj(pid, text)],
        "includes": {"users": [{"id": ELON_ID, "username": "elonmusk", "name": "Elon Musk"}]},
        "meta": {"newest_id": pid, "oldest_id": pid, "result_count": 1},
    }


class FakeX:
    """Plays the X API v2 endpoints ceoscan uses."""

    def __init__(self) -> None:
        self.base_url = ""
        self.events: list[str] = []
        self.rules: list[dict[str, Any]] = []
        self.rule_posts: list[dict[str, Any]] = []
        self.forbid_rules = False
        self.reject_rules = False
        self.probe_status = 200
        self.stream_script = "posts"  # posts | not_enrolled | drop_then_keepalive
        self.stream_posts: list[dict[str, Any]] = []
        self.stream_connects = 0
        self.timeline: dict[str, list[dict[str, Any]]] = {}
        self.timeline_requests: list[dict[str, str]] = []
        self.users: list[dict[str, Any]] = []
        self.users_status = 200  # e.g. 402 -> every /2/users/by call fails with CreditsDepleted
        self.reject_token = False  # 401 on every endpoint
        self.auth_headers: set[str] = set()
        self.release = asyncio.Event()

    def _seen(self, request: web.Request) -> None:
        self.auth_headers.add(request.headers.get("Authorization", ""))
        if self.reject_token:
            raise web.HTTPUnauthorized(
                text=json.dumps(
                    {"title": "Unauthorized", "type": "about:blank", "status": 401, "detail": "Unauthorized"}
                ),
                content_type="application/json",
            )

    async def get_rules(self, request: web.Request) -> web.StreamResponse:
        self._seen(request)
        self.events.append("rules_get")
        if self.forbid_rules:
            return web.json_response(NOT_ENROLLED, status=403)
        return web.json_response({"data": self.rules, "meta": {"result_count": len(self.rules)}})

    async def post_rules(self, request: web.Request) -> web.StreamResponse:
        self._seen(request)
        body = await request.json()
        dry = request.query.get("dry_run") == "true"
        self.rule_posts.append({"dry_run": dry, "body": body})
        if "delete" in body:
            ids = set(body["delete"]["ids"])
            if not dry:
                self.rules = [r for r in self.rules if r["id"] not in ids]
            self.events.append("rules_delete" + ("_dry" if dry else ""))
            return web.json_response({"meta": {"summary": {"deleted": len(ids), "not_deleted": 0}}})
        adds = body["add"]
        self.events.append("rules_add" + ("_dry" if dry else ""))
        if self.reject_rules:
            errors = [{"title": "UnprocessableEntity", "value": r["value"], "details": ["bad operator"]} for r in adds]
            return web.json_response(
                {"errors": errors, "meta": {"summary": {"created": 0, "not_created": len(adds), "valid": 0,
                                                        "invalid": len(adds)}}}
            )
        created = [{"id": str(1000 + len(self.rules) + i), **r} for i, r in enumerate(adds)]
        if not dry:
            self.rules.extend(created)
        summary = {"created": 0 if dry else len(adds), "not_created": 0, "valid": len(adds), "invalid": 0}
        return web.json_response({"data": created, "meta": {"summary": summary}})

    async def usage(self, request: web.Request) -> web.StreamResponse:
        self._seen(request)
        return web.json_response(
            {"data": {"cap_reset_day": 19, "project_cap": "3000000", "project_id": "1", "project_usage": "42"}}
        )

    async def stream(self, request: web.Request) -> web.StreamResponse:
        self._seen(request)
        if not request.query:  # check_access probe (no parameters)
            self.events.append("probe")
            if self.probe_status == 200:
                return web.Response(status=200, text="")
            return web.json_response(NOT_ENROLLED, status=self.probe_status)
        self.stream_connects += 1
        self.events.append("stream_connect")
        if self.stream_script == "not_enrolled":
            return web.json_response(NOT_ENROLLED, status=403)
        resp = web.StreamResponse(status=200, headers={"Content-Type": "application/json"})
        await resp.prepare(request)
        try:
            await resp.write(b"\r\n")
            if self.stream_script == "drop_then_keepalive" and self.stream_connects == 1:
                await resp.write_eof()  # X dropped the connection
                return resp
            for p in self.stream_posts:
                await resp.write(json.dumps(p).encode() + b"\r\n")
            while not self.release.is_set():
                await resp.write(b"\r\n")
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(self.release.wait(), 0.05)
        except (ConnectionError, aiohttp.ClientConnectionResetError, RuntimeError):
            pass
        return resp

    async def user_tweets(self, request: web.Request) -> web.StreamResponse:
        self._seen(request)
        uid = request.match_info["uid"]
        self.timeline_requests.append({"uid": uid, **request.query})
        queue = self.timeline.get(uid) or []
        if queue:
            return web.json_response(queue.pop(0))
        return web.json_response({"meta": {"result_count": 0}})

    async def users_by(self, request: web.Request) -> web.StreamResponse:
        self._seen(request)
        if self.users_status == 402:
            return web.json_response(
                {
                    "title": "CreditsDepleted",
                    "detail": "Your enrolled account [123] does not have any credits to fulfill this request.",
                    "type": "https://api.twitter.com/2/problems/credits",
                },
                status=402,
            )
        wanted = request.query.get("usernames", "").split(",")
        known = {u["username"].lower(): u for u in self.users}
        data = [known[w.lower()] for w in wanted if w.lower() in known]
        errors = [
            {"value": w, "detail": f"Could not find user with usernames: [{w}].", "title": "Not Found Error",
             "resource_type": "user", "parameter": "usernames", "type": "https://api.twitter.com/2/problems/resource-not-found"}
            for w in wanted
            if w.lower() not in known
        ]
        body: dict[str, Any] = {"data": data}
        if errors:
            body["errors"] = errors
        return web.json_response(body)

    async def users_by_ids(self, request: web.Request) -> web.StreamResponse:
        self._seen(request)
        wanted = request.query.get("ids", "").split(",")
        known = {u["id"]: u for u in self.users}
        return web.json_response({"data": [known[w] for w in wanted if w in known]})

    async def connections(self, request: web.Request) -> web.StreamResponse:
        self._seen(request)
        return web.json_response({"data": {"killed_connections": True}})


@pytest.fixture
async def fake_x():
    srv = FakeX()
    app = web.Application()
    app.router.add_get("/2/tweets/search/stream/rules", srv.get_rules)
    app.router.add_post("/2/tweets/search/stream/rules", srv.post_rules)
    app.router.add_get("/2/tweets/search/stream", srv.stream)
    app.router.add_get("/2/usage/tweets", srv.usage)
    app.router.add_get("/2/users/by", srv.users_by)
    app.router.add_get("/2/users", srv.users_by_ids)
    app.router.add_get("/2/users/{uid}/tweets", srv.user_tweets)
    app.router.add_delete("/2/connections/filtered_stream", srv.connections)
    server = TestServer(app, host="127.0.0.1")
    await server.start_server()
    srv.base_url = str(server.make_url("")).rstrip("/")
    try:
        yield srv
    finally:
        srv.release.set()
        await server.close()


def make_cfg(
    tmp_path: Path,
    base_url: str,
    *,
    mode: str = "auto",
    min_gap_seconds: float = 20.0,
    keyword_rules: tuple[KeywordRule, ...] = (),
) -> AppConfig:
    s = Settings()
    s.x_api.mode = mode
    s.x_api.base_url = base_url
    s.x_api.recovery.min_gap_seconds = min_gap_seconds
    s.alerts.quiet_hours.enabled = False
    s.ops.daily_summary.enabled = False
    s.ops.db_path = str(tmp_path / "ceoscan.db")
    s.ops.health_host = "127.0.0.1"
    s.ops.health_port = 0
    wl = Watchlist(
        accounts=(
            Account("elonmusk", ("TSLA",), user_id=ELON_ID, always_alert=True, expect_name="Elon Musk"),
            Account("LisaSu", ("AMD",), expect_name="Lisa Su"),  # unverified: skipped
        ),
        keyword_rules=keyword_rules,
        my_tickers=("TSLA", "AMD"),
        company_names={"TSLA": ("Tesla",), "AMD": ("AMD",)},
    )
    return AppConfig(
        settings=s,
        watchlist=wl,
        secrets=Secrets(x_bearer_token=TOKEN),
        settings_path=tmp_path / "settings.yaml",
        watchlist_path=tmp_path / "watchlist.yaml",
    )


@pytest.fixture
def capture(monkeypatch: pytest.MonkeyPatch) -> CaptureNotifier:
    notifier = CaptureNotifier("capture")
    _patch_dispatcher(monkeypatch, app_mod, push=[notifier])
    return notifier


async def wait_until(cond: Callable[[], bool], task: asyncio.Task[Any] | None = None, timeout: float = 10.0) -> None:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while not cond():
        if task is not None and task.done():
            raise AssertionError(f"service exited early with {task.result()!r}")
        if loop.time() > deadline:
            raise AssertionError("timed out waiting for condition")
        await asyncio.sleep(0.02)


def post_alerts(n: CaptureNotifier) -> list[Any]:
    return [m for m in n.sent if m.kind == "post"]


def system_titles(n: CaptureNotifier) -> list[str]:
    return [m.title for m in n.sent if m.kind == "system"]


async def stop_service(svc: Service, task: asyncio.Task[int]) -> int:
    svc.request_stop()
    return await asyncio.wait_for(task, timeout=20)


# --------------------------------------------------------------------------- Service: stream mode


async def test_service_stream_mode_end_to_end(tmp_path: Path, fake_x: FakeX, capture: CaptureNotifier) -> None:
    pid = fresh_id()
    fake_x.stream_posts = [stream_payload(pid, "Tesla raises guidance: $TSLA deliveries up 25% to 500,000 units")]
    cfg = make_cfg(tmp_path, fake_x.base_url)
    lines: list[str] = []
    svc = Service(cfg, out=lines.append, install_signal_handlers=False)
    task = asyncio.create_task(svc.run())
    await wait_until(lambda: bool(post_alerts(capture)), task)

    # health server is wired to the live status
    assert svc.health is not None
    async with aiohttp.ClientSession() as s, s.get(f"http://127.0.0.1:{svc.health.port}/health") as resp:
        assert resp.status == 200
        health = await resp.json()
    assert health["mode"] == "stream"
    assert health["stream"]["state"] == "connected"

    code = await stop_service(svc, task)
    assert code == EXIT_OK
    out = "\n".join(lines)
    assert ALERTS_ONLY_BANNER in out
    assert "X API access check" in out
    assert "Estimated monthly cost" in out and "Total:" in out
    assert TOKEN not in out

    # rules were validated (dry run) and installed before the stream connected
    assert "rules_add_dry" in fake_x.events and "rules_add" in fake_x.events
    assert fake_x.events.index("rules_add") < fake_x.events.index("stream_connect")
    assert [r["value"] for r in fake_x.rules] == [f"from:{ELON_ID} -is:retweet"]
    assert fake_x.auth_headers == {f"Bearer {TOKEN}"}

    alert = post_alerts(capture)[0]
    assert "TSLA" in alert.title and "@elonmusk" in alert.title
    started = [t for t in system_titles(capture) if t.startswith("ceoscan started")]
    assert started == ["ceoscan started (stream, 1 accounts, est. 4.5s)"]
    assert svc.status.state == "stopped"

    store = Store(cfg.settings.ops.db_path)
    try:
        row = store.get_post_row(pid)
        assert row is not None and row["source"] == "stream"
        assert store.alert_exists_for_post(pid)
    finally:
        store.close()


async def test_service_gap_recovery_after_reconnect(tmp_path: Path, fake_x: FakeX, capture: CaptureNotifier) -> None:
    fake_x.stream_script = "drop_then_keepalive"
    pid = fresh_id()
    fake_x.timeline[ELON_ID] = [timeline_response(pid, "Tesla Q3 earnings beat: $TSLA EPS up 40%")]
    cfg = make_cfg(tmp_path, fake_x.base_url, min_gap_seconds=0.0)
    svc = Service(cfg, out=lambda _l: None, install_signal_handlers=False)
    task = asyncio.create_task(svc.run())
    await wait_until(lambda: bool(post_alerts(capture)), task)
    assert await stop_service(svc, task) == EXIT_OK

    assert fake_x.stream_connects >= 2
    assert fake_x.timeline_requests, "poller.recover() should have fetched the timeline"
    req = fake_x.timeline_requests[0]
    assert req["uid"] == ELON_ID and int(req["since_id"]) > 0
    store = Store(cfg.settings.ops.db_path)
    try:
        assert store.get_post_row(pid)["source"] == "recovery"
    finally:
        store.close()


# --------------------------------------------------------------------------- Service: polling


async def test_service_falls_back_to_polling_when_stream_unavailable(
    tmp_path: Path, fake_x: FakeX, capture: CaptureNotifier
) -> None:
    fake_x.stream_script = "not_enrolled"  # the probe says OK, the real connect says "no access"
    pid = fresh_id()
    fake_x.timeline[ELON_ID] = [timeline_response(pid, "Tesla $TSLA announces a $5B buyback")]
    cfg = make_cfg(tmp_path, fake_x.base_url, keyword_rules=(KeywordRule("big", ("TSLA",)),))
    lines: list[str] = []
    svc = Service(cfg, out=lines.append, install_signal_handlers=False)
    task = asyncio.create_task(svc.run())
    await wait_until(lambda: bool(post_alerts(capture)), task)
    assert svc.status.mode == "poll"
    assert await stop_service(svc, task) == EXIT_OK

    out = "\n".join(lines)
    assert "falling back to timeline polling" in out
    assert "each checked every 2.0s" in out and "est. latency typical 3.0s, worst 4.0s" in out
    assert "keyword rule(s) need the filtered stream" in out
    assert "ceoscan switched to timeline polling" in system_titles(capture)
    assert svc.mode == "poll"
    req = fake_x.timeline_requests[0]
    assert req["exclude"] == "retweets"  # include_replies defaults to true
    store = Store(cfg.settings.ops.db_path)
    try:
        assert store.get_post_row(pid)["source"] == "poll"
    finally:
        store.close()


async def test_service_polls_when_access_check_finds_no_stream(
    tmp_path: Path, fake_x: FakeX, capture: CaptureNotifier
) -> None:
    fake_x.forbid_rules = True
    fake_x.probe_status = 403
    pid = fresh_id()
    fake_x.timeline[ELON_ID] = [timeline_response(pid, "Tesla $TSLA guidance raised")]
    cfg = make_cfg(tmp_path, fake_x.base_url)
    lines: list[str] = []
    svc = Service(cfg, out=lines.append, install_signal_handlers=False)
    task = asyncio.create_task(svc.run())
    await wait_until(lambda: bool(post_alerts(capture)), task)
    assert await stop_service(svc, task) == EXIT_OK

    assert svc.access_report is not None and svc.access_report.mode == "poll"
    assert fake_x.stream_connects == 0 and not fake_x.rule_posts  # no rule sync, no stream
    assert "Timeline polling: 1 account(s)" in "\n".join(lines)
    assert any(t.startswith("ceoscan started (poll, 1 accounts") for t in system_titles(capture))


async def test_forced_stream_mode_without_stream_access_exits_2(
    tmp_path: Path, fake_x: FakeX, capture: CaptureNotifier
) -> None:
    fake_x.stream_script = "not_enrolled"
    cfg = make_cfg(tmp_path, fake_x.base_url, mode="stream")
    svc = Service(cfg, out=lambda _l: None, install_signal_handlers=False)
    assert await asyncio.wait_for(svc.run(), timeout=20) == EXIT_CONFIG
    assert not fake_x.timeline_requests


async def test_rejected_stream_rules_exit_2(tmp_path: Path, fake_x: FakeX, capture: CaptureNotifier) -> None:
    fake_x.reject_rules = True
    cfg = make_cfg(tmp_path, fake_x.base_url)
    svc = Service(cfg, out=lambda _l: None, install_signal_handlers=False)
    assert await asyncio.wait_for(svc.run(), timeout=20) == EXIT_CONFIG
    assert fake_x.stream_connects == 0
    assert all(p["dry_run"] for p in fake_x.rule_posts)  # nothing was changed on X


async def test_sigterm_shuts_down_gracefully(tmp_path: Path, fake_x: FakeX, capture: CaptureNotifier) -> None:
    cfg = make_cfg(tmp_path, fake_x.base_url)
    svc = Service(cfg, out=lambda _l: None, install_signal_handlers=True)
    task = asyncio.create_task(svc.run())
    await wait_until(lambda: svc.status.state == "connected", task)
    os.kill(os.getpid(), signal.SIGTERM)
    assert await asyncio.wait_for(task, timeout=20) == EXIT_OK
    assert svc.stop.is_set()
    assert signal.getsignal(signal.SIGTERM) == signal.SIG_DFL  # handler removed again


# --------------------------------------------------------------------------- CLI commands against fake X


async def test_rules_command_shows_plan_and_dry_run(
    tmp_path: Path, fake_x: FakeX, capsys: pytest.CaptureFixture[str]
) -> None:
    fake_x.rules = [
        {"id": "1", "value": "from:1 -is:retweet", "tag": "ceoscan:acct:r1t0:1"},  # stale (ours)
        {"id": "2", "value": "$GME", "tag": "my-own-rule"},  # foreign: kept
    ]
    cfg = make_cfg(tmp_path, fake_x.base_url)
    code = await cli.cmd_rules(cfg, cli.build_parser().parse_args(["rules"]))
    assert code == 0
    out = capsys.readouterr().out
    assert f"from:{ELON_ID} -is:retweet" in out
    assert "@LisaSu" in out  # skipped (unverified) account
    assert "1 to add, 1 to delete, 1 foreign (kept)" in out
    assert "Dry-run validation: X accepted all 1 new rule(s)." in out
    assert [p["dry_run"] for p in fake_x.rule_posts] == [True]
    assert len(fake_x.rules) == 2  # nothing changed without --apply


async def test_rules_apply_syncs(tmp_path: Path, fake_x: FakeX, capsys: pytest.CaptureFixture[str]) -> None:
    fake_x.rules = [{"id": "1", "value": "from:1 -is:retweet", "tag": "ceoscan:acct:r1t0:1"}]
    cfg = make_cfg(tmp_path, fake_x.base_url)
    code = await cli.cmd_rules(cfg, cli.build_parser().parse_args(["rules", "--apply"]))
    assert code == 0
    assert "Applied: deleted 1 rule(s), added 1 rule(s)." in capsys.readouterr().out
    assert [r["value"] for r in fake_x.rules] == [f"from:{ELON_ID} -is:retweet"]


async def test_rules_command_reports_rejected_rules(
    tmp_path: Path, fake_x: FakeX, capsys: pytest.CaptureFixture[str]
) -> None:
    fake_x.reject_rules = True
    cfg = make_cfg(tmp_path, fake_x.base_url)
    assert await cli.cmd_rules(cfg, cli.build_parser().parse_args(["rules"])) == 1
    assert "Dry-run validation FAILED" in capsys.readouterr().out


async def test_check_access_command(tmp_path: Path, fake_x: FakeX, capsys: pytest.CaptureFixture[str]) -> None:
    cfg = make_cfg(tmp_path, fake_x.base_url)
    assert await cli.cmd_check_access(cfg, cli.build_parser().parse_args(["check-access"])) == 0
    out = capsys.readouterr().out
    assert "X API access check" in out and "mode:             stream" in out
    assert "42 / 3,000,000" in out


async def test_verify_write_stores_user_ids(
    tmp_path: Path, fake_x: FakeX, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    settings = tmp_path / "settings.yaml"
    settings.write_text(
        f"x_api:\n  base_url: {fake_x.base_url}\nops:\n  db_path: {tmp_path / 'ceoscan.db'}\n", encoding="utf-8"
    )
    watchlist = tmp_path / "watchlist.yaml"
    watchlist.write_text(
        "# my watchlist\naccounts:\n"
        "  - handle: elonmusk   # Tesla\n    expect_name: Elon Musk\n    tickers: [TSLA]\n"
        "  - handle: NoSuchCeo\n    tickers: [AMD]\n",
        encoding="utf-8",
    )
    fake_x.users = [
        {
            "id": ELON_ID,
            "username": "elonmusk",
            "name": "Elon Musk",
            "verified": True,
            "verified_type": "blue",
            "protected": False,
            "parody": False,
            "public_metrics": {"followers_count": 200_000_000, "tweet_count": 90_000},
            "most_recent_tweet_id": fresh_id(),
        }
    ]
    monkeypatch.setenv("X_BEARER_TOKEN", TOKEN)
    cfg = load_app_config(settings, watchlist, tmp_path / "no.env")
    code = await cli.cmd_verify(cfg, cli.build_parser().parse_args(["verify", "--write"]))
    assert code == 0
    out = capsys.readouterr().out
    assert "NOT FOUND" in out
    assert "Updated 1 account(s)" in out
    assert "1 of 2 account(s) now have a user_id" in out
    wl = load_watchlist(watchlist)
    assert {a.handle: a.user_id for a in wl.accounts} == {"elonmusk": ELON_ID, "NoSuchCeo": None}
    assert "# Tesla" in watchlist.read_text(encoding="utf-8")  # comments survive


def _verify_files(tmp_path: Path, fake_x: FakeX, elon_user_id: str | None = None) -> tuple[Path, Path]:
    settings = tmp_path / "settings.yaml"
    settings.write_text(
        f"x_api:\n  base_url: {fake_x.base_url}\nops:\n  db_path: {tmp_path / 'ceoscan.db'}\n", encoding="utf-8"
    )
    watchlist = tmp_path / "watchlist.yaml"
    pinned = f'    user_id: "{elon_user_id}"\n' if elon_user_id else ""
    watchlist.write_text(
        f"accounts:\n  - handle: elonmusk\n{pinned}    expect_name: Elon Musk\n    tickers: [TSLA]\n"
        "  - handle: LisaSu\n    expect_name: Lisa Su\n    tickers: [AMD]\n",
        encoding="utf-8",
    )
    return settings, watchlist


def _elon_user(uid: str = ELON_ID) -> dict[str, Any]:
    return {
        "id": uid,
        "username": "elonmusk",
        "name": "Elon Musk",
        "verified": True,
        "verified_type": "blue",
        "protected": False,
        "parody": False,
        "created_at": "2009-06-02T20:12:29.000Z",
        "public_metrics": {"followers_count": 200_000_000, "tweet_count": 90_000},
        "most_recent_tweet_id": fresh_id(),
    }


async def test_verify_credits_depleted_prints_one_clear_message(
    tmp_path: Path, fake_x: FakeX, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """REQ-7: HTTP 402 for every handle -> one line with the fix, no table, no 'up to date', exit 1."""
    fake_x.users_status = 402
    settings, watchlist = _verify_files(tmp_path, fake_x)
    before = watchlist.read_text(encoding="utf-8")
    monkeypatch.setenv("X_BEARER_TOKEN", TOKEN)
    cfg = load_app_config(settings, watchlist, tmp_path / "no.env")
    code = await cli.cmd_verify(cfg, cli.build_parser().parse_args(["verify", "--write"]))
    assert code == 1
    captured = capsys.readouterr()
    assert captured.err.count("add credits at console.x.com") == 1
    assert "No handles were checked, nothing was billed and nothing was written." in captured.err
    assert "up to date" not in captured.out and "STATUS" not in captured.out
    assert watchlist.read_text(encoding="utf-8") == before


async def test_verify_never_silently_changes_a_pinned_user_id(
    tmp_path: Path, fake_x: FakeX, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """SEC-6: the handle now returns another id -> warning; only --accept-warnings writes it, loudly."""
    fake_x.users = [_elon_user()]
    settings, watchlist = _verify_files(tmp_path, fake_x, elon_user_id="111")
    monkeypatch.setenv("X_BEARER_TOKEN", TOKEN)
    cfg = load_app_config(settings, watchlist, tmp_path / "no.env")
    assert await cli.cmd_verify(cfg, cli.build_parser().parse_args(["verify", "--write"])) == 0
    out = capsys.readouterr().out
    assert f"CHANGED @elonmusk: pinned 111 -> X now returns {ELON_ID}" in out
    assert f"NOT changed: @elonmusk keeps user_id 111 (X now returns {ELON_ID})." in out
    assert {a.handle: a.user_id for a in load_watchlist(watchlist).accounts}["elonmusk"] == "111"

    cfg = load_app_config(settings, watchlist, tmp_path / "no.env")
    args = cli.build_parser().parse_args(["verify", "--write", "--accept-warnings"])
    assert await cli.cmd_verify(cfg, args) == 0
    out = capsys.readouterr().out
    assert f"CHANGED user_id of @elonmusk: 111 -> {ELON_ID}" in out
    assert {a.handle: a.user_id for a in load_watchlist(watchlist).accounts}["elonmusk"] == ELON_ID


async def test_verify_renamed_handle_keeps_pinned_id(
    tmp_path: Path, fake_x: FakeX, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """REQ-4: not found by handle, but the pinned id now has another handle -> keep the id."""
    renamed = {**_elon_user(), "username": "elon_new"}
    fake_x.users = [renamed]
    settings, watchlist = _verify_files(tmp_path, fake_x, elon_user_id=ELON_ID)
    monkeypatch.setenv("X_BEARER_TOKEN", TOKEN)
    cfg = load_app_config(settings, watchlist, tmp_path / "no.env")
    assert await cli.cmd_verify(cfg, cli.build_parser().parse_args(["verify", "--write"])) == 0
    out = capsys.readouterr().out
    assert "now uses @elon_new (renamed)" in out
    assert {a.handle: a.user_id for a in load_watchlist(watchlist).accounts}["elonmusk"] == ELON_ID


async def test_check_access_rejected_token_exits_2(
    tmp_path: Path, fake_x: FakeX, capsys: pytest.CaptureFixture[str]
) -> None:
    """SEC-10: a wrong bearer token must not exit 0."""
    fake_x.reject_token = True
    cfg = make_cfg(tmp_path, fake_x.base_url)
    assert await cli.cmd_check_access(cfg, cli.build_parser().parse_args(["check-access"])) == 2
    captured = capsys.readouterr()
    assert "rejected the bearer token" in captured.out
    assert "X_BEARER_TOKEN" in captured.err


# --------------------------------------------------------------------------- run: configuration cannot be loaded


def _broken_settings(tmp_path: Path, extra: str = "") -> Path:
    p = tmp_path / "settings.yaml"
    p.write_text(f"alerts:\n  timezone: America/LosAngeles\nops:\n  db_path: {tmp_path / 'c.db'}\n{extra}")
    return p


def test_run_with_broken_settings_alerts_once_and_exits_2(
    no_network: None,
    missing_env: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """SEC-3 / SEC-8: a typo is caught at load time, alerted once (deduped), and `run` exits 2."""
    push = CaptureNotifier("ntfy")
    _patch_dispatcher(monkeypatch, cli, push=[push])
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("CEOSCAN_CONFIG_ERROR_ACTION", "exit")
    # a broken settings file: the fallback (defaults) keeps data/ in the working directory
    settings = _broken_settings(tmp_path)
    argv = ["--settings", str(settings), "--watchlist", str(WATCHLIST), "--env", missing_env, "run"]
    assert cli.main(argv) == 2
    assert "unknown time zone 'America/LosAngeles'" in capsys.readouterr().err
    assert len(push.sent) == 1
    assert push.sent[0].title.startswith("ceoscan stopped: configuration problem: alerts.timezone")
    assert push.sent[0].priority == "high"
    assert cli.main(argv) == 2  # a restart with the same problem: no second alert
    assert len(push.sent) == 1


async def test_run_config_error_wait_mode_idles_with_health_503(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """SEC-8: with config_error_action wait (Docker) `run` stays up instead of restart-looping."""
    push = CaptureNotifier("ntfy")
    _patch_dispatcher(monkeypatch, cli, push=[push])
    settings = tmp_path / "settings.yaml"
    settings.write_text(
        f"ops:\n  db_path: {tmp_path / 'c.db'}\n  health_port: 0\n  config_error_action: wait\n", encoding="utf-8"
    )
    argv = ["--settings", str(settings), "--watchlist", str(tmp_path / "nope.yaml"), "run"]
    args = cli.build_parser().parse_args(argv)
    stop = asyncio.Event()
    started: list[cli.HealthServer] = []
    real_start = cli.HealthServer.start

    async def spy_start(self: cli.HealthServer) -> None:
        await real_start(self)
        started.append(self)

    monkeypatch.setattr(cli.HealthServer, "start", spy_start)
    task = asyncio.create_task(
        cli.run_config_error(args, "watchlist missing", stop=stop, install_signal_handlers=False)
    )
    for _ in range(200):
        if started:
            break
        await asyncio.sleep(0.01)
    assert started and not task.done()
    srv = started[0]
    async with aiohttp.ClientSession() as session:
        async with session.get(f"http://127.0.0.1:{srv.port}/health") as resp:
            assert resp.status == 503
            body = await resp.json()
    assert body["state"] == "config_error" and body["status"] == "down"
    assert "watchlist missing" in body["reasons"][0]
    assert len(push.sent) == 1 and "idle" in push.sent[0].body
    stop.set()
    assert await asyncio.wait_for(task, timeout=5) == 2


def test_dotenv_permission_denied_is_not_fatal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """SEC-2: systemd already exported the secrets; an unreadable .env must not stop the service."""
    env = tmp_path / ".env"
    env.write_text("X_BEARER_TOKEN=from-file\n", encoding="utf-8")
    env.chmod(0)
    if os.access(env, os.R_OK):  # running as root: simulate the permission error instead
        import builtins

        real_open = builtins.open

        def fake_open(file: Any, *a: Any, **k: Any) -> Any:
            if str(file) == str(env):
                raise PermissionError(13, "Permission denied", str(file))
            return real_open(file, *a, **k)

        monkeypatch.setattr(builtins, "open", fake_open)
    monkeypatch.setenv("X_BEARER_TOKEN", TOKEN)
    assert cli.main(["--settings", str(SETTINGS), "--watchlist", str(WATCHLIST), "--env", str(env), "cost"]) == 0
    err = capsys.readouterr().err
    assert f"warning: cannot read {env} (Permission denied); using the existing environment" in err
    assert TOKEN not in err
