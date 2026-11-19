"""Notifiers (ntfy, Pushover, Twilio, email, console, capture) and the AlertDispatcher.

All HTTP is mocked with aioresponses; SMTP is replaced with a fake. No real network.
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import smtplib
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import aiohttp
import aioresponses.core as aioresponses_core
import pytest
from aioresponses import aioresponses
from yarl import URL

from ceoscan import USER_AGENT
from ceoscan.alerts.dispatcher import AlertDispatcher, build_dispatcher
from ceoscan.alerts.notifiers import (
    PUSHOVER_URL,
    CaptureNotifier,
    ConsoleNotifier,
    EmailNotifier,
    Notifier,
    NtfyNotifier,
    PushoverNotifier,
    TwilioSmsNotifier,
)
from ceoscan.config import AlertSettings, AppConfig, EmailSettings, PushoverSettings, Secrets, Settings
from ceoscan.models import AlertMessage, ChannelResult, Watchlist

NTFY_TOPIC = "ceoscan-topic-SECRET-9f8e7d"
NTFY_TOKEN = "tk_ntfysecrettoken123456"
PO_APP = "test-pushover-app-token-fake01"
PO_USER = "test-pushover-user-key-fake001"
TW_SID = "ACtest-fake-account-sid-not-real"
TW_TOKEN = "twilio-auth-token-secret-xyz"
TW_FROM = "+15005550006"
TW_TO = "+14155550123"
SMTP_PASS = "smtp-password-secret-123"
TWILIO_URL = f"https://api.twilio.com/2010-04-01/Accounts/{TW_SID}/Messages.json"

ALL_SECRETS = [NTFY_TOPIC, NTFY_TOKEN, PO_APP, PO_USER, TW_SID, TW_TOKEN, SMTP_PASS]


class _CompatClientResponse(aiohttp.ClientResponse):
    """aioresponses 0.7.9 does not pass ``stream_writer`` (required by the pinned aiohttp 3.14)."""

    def __init__(self, method: str, url: Any, **kwargs: Any) -> None:
        kwargs.setdefault("stream_writer", SimpleNamespace(output_size=0))
        super().__init__(method, url, **kwargs)


@pytest.fixture(autouse=True)
def _aioresponses_compat(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(aioresponses_core, "ClientResponse", _CompatClientResponse)


@pytest.fixture
async def session():
    async with aiohttp.ClientSession() as s:
        yield s


def post_msg(priority: str = "high", **kw: Any) -> AlertMessage:
    defaults: dict[str, Any] = dict(
        title="🟢 TSLA +8/10 @elonmusk",
        body="@elonmusk · TSLA · BULLISH · 8/10\nWhy: big news\nhttps://x.com/elonmusk/status/1",
        url="https://x.com/elonmusk/status/1",
        priority=priority,
        tags=["TSLA", "bullish"],
        sms_text="@elonmusk TSLA BULLISH 8/10: big news https://x.com/elonmusk/status/1",
        kind="post",
    )
    defaults.update(kw)
    return AlertMessage(**defaults)


def only_call(m: aioresponses, method: str, url: str) -> dict[str, Any]:
    calls = m.requests[(method, URL(url))]
    assert len(calls) == 1
    return calls[0].kwargs


def assert_no_secrets(text: str | None) -> None:
    for s in ALL_SECRETS:
        assert s not in (text or "")


# =========================================================================== ntfy


async def test_ntfy_json_publish_to_server_root(session) -> None:
    n = NtfyNotifier(session, "https://ntfy.sh", NTFY_TOPIC, None, timeout=3)
    with aioresponses() as m:
        m.post("https://ntfy.sh/", status=200, payload={"id": "abc", "event": "message"})
        res = await n.send(post_msg())
        kw = only_call(m, "POST", "https://ntfy.sh/")
    assert res == ChannelResult("ntfy", True, res.elapsed_ms, status=200)
    assert kw["json"] == {
        "topic": NTFY_TOPIC,
        "title": "🟢 TSLA +8/10 @elonmusk",  # UTF-8/emoji title travels in the JSON body
        "message": post_msg().body,
        "priority": 4,
        "click": "https://x.com/elonmusk/status/1",
        "tags": ["TSLA", "bullish"],
    }
    assert "Authorization" not in kw["headers"]
    assert kw["headers"]["User-Agent"] == USER_AGENT
    assert kw["timeout"].total == 3


async def test_ntfy_bearer_token_and_custom_server(session) -> None:
    n = NtfyNotifier(session, "https://ntfy.example.com/", NTFY_TOPIC, NTFY_TOKEN, timeout=3)
    with aioresponses() as m:
        m.post("https://ntfy.example.com/", status=200, payload={})
        res = await n.send(post_msg(url=None, tags=[]))
        kw = only_call(m, "POST", "https://ntfy.example.com/")
    assert res.ok
    assert kw["headers"]["Authorization"] == f"Bearer {NTFY_TOKEN}"
    assert "click" not in kw["json"] and "tags" not in kw["json"]


@pytest.mark.parametrize(("priority", "expected"), [("low", 2), ("normal", 3), ("high", 4), ("urgent", 5)])
async def test_ntfy_priority_mapping(session, priority: str, expected: int) -> None:
    n = NtfyNotifier(session, "https://ntfy.sh", NTFY_TOPIC, None, timeout=3)
    assert n.payload(post_msg(priority=priority))["priority"] == expected


async def test_ntfy_http_error_no_secrets(session) -> None:
    n = NtfyNotifier(session, "https://ntfy.sh", NTFY_TOPIC, NTFY_TOKEN, timeout=3)
    body = {"code": 40301, "http": 403, "error": f"forbidden for topic {NTFY_TOPIC}", "link": "https://ntfy.sh/docs"}
    with aioresponses() as m:
        m.post("https://ntfy.sh/", status=403, body=json.dumps(body), content_type="application/json")
        res = await n.send(post_msg())
    assert res.ok is False
    assert res.status == 403
    assert res.error is not None and res.error.startswith("HTTP 403 forbidden")
    assert_no_secrets(res.error)


async def test_ntfy_timeout(session) -> None:
    n = NtfyNotifier(session, "https://ntfy.sh", NTFY_TOPIC, None, timeout=2)
    with aioresponses() as m:
        m.post("https://ntfy.sh/", exception=asyncio.TimeoutError())
        res = await n.send(post_msg())
    assert not res.ok and res.status is None
    assert res.error == "timeout after 2s"


async def test_ntfy_connection_error(session) -> None:
    n = NtfyNotifier(session, "https://ntfy.sh", NTFY_TOPIC, None, timeout=2)
    with aioresponses() as m:
        m.post("https://ntfy.sh/", exception=aiohttp.ClientConnectionError(f"boom {NTFY_TOPIC}"))
        res = await n.send(post_msg())
    assert not res.ok
    assert res.error is not None and "ClientConnectionError" in res.error
    assert_no_secrets(res.error)


# =========================================================================== Pushover


async def test_pushover_form_fields(session) -> None:
    n = PushoverNotifier(session, PO_APP, PO_USER, PushoverSettings(sound="cashregister"), timeout=4)
    with aioresponses() as m:
        m.post(PUSHOVER_URL, status=200, payload={"status": 1, "request": "r1"})
        res = await n.send(post_msg())
        kw = only_call(m, "POST", PUSHOVER_URL)
    assert res.ok and res.channel == "pushover" and res.status == 200
    assert kw["data"] == {
        "token": PO_APP,
        "user": PO_USER,
        "title": "🟢 TSLA +8/10 @elonmusk",
        "message": post_msg().body,
        "priority": "0",
        "url": "https://x.com/elonmusk/status/1",
        "url_title": "Open post",
        "sound": "cashregister",
    }
    assert "json" not in kw


async def test_pushover_truncates_title_and_message(session) -> None:
    n = PushoverNotifier(session, PO_APP, PO_USER, PushoverSettings(), timeout=4)
    form = n.form(post_msg(title="T" * 400, body="B" * 3000))
    assert len(form["title"]) <= 250
    assert len(form["message"]) <= 1024
    assert form["message"].endswith("…")
    assert "sound" not in form


@pytest.mark.parametrize(("priority", "expected"), [("low", -1), ("normal", 0), ("high", 0), ("urgent", 1)])
async def test_pushover_priority_mapping(session, priority: str, expected: int) -> None:
    n = PushoverNotifier(session, PO_APP, PO_USER, PushoverSettings(), timeout=4)
    assert n.priority(post_msg(priority=priority)) == expected
    assert n.priority(post_msg(priority=priority, kind="system")) == expected


async def test_pushover_high_priority_min_score(session) -> None:
    n = PushoverNotifier(session, PO_APP, PO_USER, PushoverSettings(high_priority_min_score=8), timeout=4)
    assert n.priority(post_msg(priority="high"), score=8) == 1
    assert n.priority(post_msg(priority="high"), score=7) == 0
    strict = PushoverNotifier(session, PO_APP, PO_USER, PushoverSettings(high_priority_min_score=10), timeout=4)
    assert strict.priority(post_msg(priority="urgent"), score=9) == 0


async def test_pushover_http_error_no_secrets(session) -> None:
    n = PushoverNotifier(session, PO_APP, PO_USER, PushoverSettings(), timeout=4)
    body = {"user": "invalid", "errors": [f"user identifier {PO_USER} is invalid"], "status": 0, "request": "r"}
    with aioresponses() as m:
        m.post(PUSHOVER_URL, status=400, payload=body)
        res = await n.send(post_msg())
    assert not res.ok and res.status == 400
    assert res.error is not None and res.error.startswith("HTTP 400 user identifier")
    assert_no_secrets(res.error)


async def test_pushover_200_with_bad_status_is_failure(session) -> None:
    n = PushoverNotifier(session, PO_APP, PO_USER, PushoverSettings(), timeout=4)
    with aioresponses() as m:
        m.post(PUSHOVER_URL, status=200, payload={"status": 0, "errors": ["nope"]})
        res = await n.send(post_msg())
    assert not res.ok and res.status == 200


# =========================================================================== Twilio


async def test_twilio_form_and_basic_auth(session) -> None:
    n = TwilioSmsNotifier(session, TW_SID, TW_TOKEN, TW_FROM, TW_TO, timeout=4)
    with aioresponses() as m:
        m.post(TWILIO_URL, status=201, payload={"sid": "SM123", "status": "queued"})
        res = await n.send(post_msg())
        kw = only_call(m, "POST", TWILIO_URL)
    assert res.ok and res.channel == "sms" and res.status == 201
    assert kw["data"] == {"To": TW_TO, "From": TW_FROM, "Body": post_msg().sms_text}
    expected = "Basic " + base64.b64encode(f"{TW_SID}:{TW_TOKEN}".encode()).decode()
    if "auth" in kw:  # aiohttp < 3.14
        assert kw["auth"].encode() == expected
    else:
        assert kw["headers"]["Authorization"] == expected
    assert kw["headers"]["User-Agent"] == USER_AGENT


async def test_twilio_falls_back_to_title(session) -> None:
    n = TwilioSmsNotifier(session, TW_SID, TW_TOKEN, TW_FROM, TW_TO, timeout=4)
    with aioresponses() as m:
        m.post(TWILIO_URL, status=201, payload={})
        await n.send(post_msg(sms_text=None))
        kw = only_call(m, "POST", TWILIO_URL)
    assert kw["data"]["Body"] == post_msg().title


async def test_twilio_http_error_no_secrets(session) -> None:
    n = TwilioSmsNotifier(session, TW_SID, TW_TOKEN, TW_FROM, TW_TO, timeout=4)
    body = {"code": 20003, "message": f"Authenticate {TW_SID}", "more_info": "https://www.twilio.com/docs/errors/20003",
            "status": 401}
    with aioresponses() as m:
        m.post(TWILIO_URL, status=401, payload=body)
        res = await n.send(post_msg())
    assert not res.ok and res.status == 401
    assert res.error is not None and res.error.startswith("HTTP 401 Authenticate") and "20003" in res.error
    assert_no_secrets(res.error)


async def test_twilio_connection_error_hides_sid_in_url(session) -> None:
    n = TwilioSmsNotifier(session, TW_SID, TW_TOKEN, TW_FROM, TW_TO, timeout=4)
    with aioresponses() as m:
        m.post(TWILIO_URL, exception=aiohttp.ClientConnectionError(f"Cannot connect to {TWILIO_URL}"))
        res = await n.send(post_msg())
    assert not res.ok
    assert_no_secrets(res.error)


# =========================================================================== email


class FakeSMTP:
    instances: list["FakeSMTP"] = []
    fail_login: bool = False

    def __init__(self, host: str, port: int, timeout: float | None = None, context: Any = None) -> None:
        self.host, self.port, self.timeout, self.context = host, port, timeout, context
        self.calls: list[str] = []
        self.sent: list[Any] = []
        FakeSMTP.instances.append(self)

    def __enter__(self) -> "FakeSMTP":
        return self

    def __exit__(self, *exc: Any) -> None:
        self.calls.append("quit")

    def ehlo(self) -> None:
        self.calls.append("ehlo")

    def starttls(self, context: Any = None) -> None:
        self.calls.append("starttls")

    def login(self, user: str, password: str) -> None:
        if FakeSMTP.fail_login:
            raise smtplib.SMTPAuthenticationError(535, f"5.7.8 bad credentials for {user} / {password}".encode())
        self.calls.append(f"login:{user}")

    def send_message(self, msg: Any) -> dict:
        self.sent.append(msg)
        self.calls.append("send")
        return {}


class FakeSMTPSSL(FakeSMTP):
    pass


@pytest.fixture
def fake_smtp(monkeypatch: pytest.MonkeyPatch) -> type[FakeSMTP]:
    FakeSMTP.instances = []
    FakeSMTP.fail_login = False
    monkeypatch.setattr(smtplib, "SMTP", FakeSMTP)
    monkeypatch.setattr(smtplib, "SMTP_SSL", FakeSMTPSSL)
    return FakeSMTP


EMAIL_SECRETS = Secrets(
    smtp_host="smtp.example.com",
    smtp_port=587,
    smtp_username="me@example.com",
    smtp_password=SMTP_PASS,
    email_from="ceoscan@example.com",
    email_to="me@example.com",
)


async def test_email_starttls_default(fake_smtp) -> None:
    n = EmailNotifier(EMAIL_SECRETS, EmailSettings(enabled=True), timeout=7)
    res = await n.send(post_msg())
    assert res.ok and res.channel == "email"
    (smtp,) = fake_smtp.instances
    assert type(smtp) is FakeSMTP
    assert (smtp.host, smtp.port, smtp.timeout) == ("smtp.example.com", 587, 7)
    assert smtp.calls == ["ehlo", "starttls", "ehlo", "login:me@example.com", "send", "quit"]
    em = smtp.sent[0]
    assert em["Subject"] == "🟢 TSLA +8/10 @elonmusk"
    assert em["From"] == "ceoscan@example.com" and em["To"] == "me@example.com"
    assert "https://x.com/elonmusk/status/1" in em.get_content()


async def test_email_ssl(fake_smtp) -> None:
    n = EmailNotifier(EMAIL_SECRETS, EmailSettings(enabled=True, use_ssl=True), timeout=7)
    res = await n.send(post_msg())
    assert res.ok
    (smtp,) = fake_smtp.instances
    assert type(smtp) is FakeSMTPSSL
    assert smtp.port == 465  # 587 (STARTTLS port) is never right for SMTPS
    assert smtp.context is not None
    assert "starttls" not in smtp.calls


async def test_email_no_login_without_credentials(fake_smtp) -> None:
    sec = Secrets(smtp_host="relay.local", smtp_port=25, email_from="a@b.c", email_to="d@e.f")
    res = await EmailNotifier(sec, EmailSettings(enabled=True), timeout=7).send(post_msg())
    assert res.ok
    assert not any(c.startswith("login") for c in fake_smtp.instances[0].calls)


async def test_email_auth_failure_no_secrets(fake_smtp) -> None:
    fake_smtp.fail_login = True
    res = await EmailNotifier(EMAIL_SECRETS, EmailSettings(enabled=True), timeout=7).send(post_msg())
    assert not res.ok
    assert res.status == 535
    assert res.error is not None and "SMTPAuthenticationError" in res.error
    assert_no_secrets(res.error)


async def test_email_connection_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(*a: Any, **k: Any) -> None:
        raise ConnectionRefusedError(111, "Connection refused")

    monkeypatch.setattr(smtplib, "SMTP", boom)
    res = await EmailNotifier(EMAIL_SECRETS, EmailSettings(enabled=True), timeout=7).send(post_msg())
    assert not res.ok and "ConnectionRefusedError" in (res.error or "")


# =========================================================================== console / capture


async def test_console_and_capture() -> None:
    out: list[str] = []
    res = await ConsoleNotifier(out=out.append).send(post_msg())
    assert res.ok and res.channel == "console"
    assert "🟢 TSLA +8/10 @elonmusk" in out[0]

    cap = CaptureNotifier()
    res = await cap.send(post_msg())
    assert res.ok and res.channel == "capture" and cap.sent == [post_msg()]
    bad = CaptureNotifier("bad", ok=False)
    assert not (await bad.send(post_msg())).ok
    assert isinstance(cap, Notifier)


# =========================================================================== dispatcher


class SlowNotifier:
    def __init__(self, name: str, delay: float) -> None:
        self.name = name
        self.delay = delay
        self.sent: list[AlertMessage] = []

    async def send(self, msg: AlertMessage) -> ChannelResult:
        await asyncio.sleep(self.delay)
        self.sent.append(msg)
        return ChannelResult(self.name, True, 0)


class RaisingNotifier:
    name = "broken"

    async def send(self, msg: AlertMessage) -> ChannelResult:
        raise RuntimeError(f"kaboom {NTFY_TOKEN}")


def dispatcher(**kw: Any) -> tuple[AlertDispatcher, CaptureNotifier, CaptureNotifier, CaptureNotifier]:
    settings = AlertSettings()
    for k, v in kw.items():
        obj, attr = settings, k.split("__")
        for a in attr[:-1]:
            obj = getattr(obj, a)
        setattr(obj, attr[-1], v)
    push, sms, email = CaptureNotifier("push"), CaptureNotifier("sms"), CaptureNotifier("email")
    return AlertDispatcher(settings, [push], sms=sms, email=email), push, sms, email


def chans(results: list[ChannelResult]) -> list[str]:
    return [r.channel for r in results]


@pytest.mark.parametrize(
    ("score", "priority", "expected"),
    [
        (7, "high", ["push", "email"]),  # email.min_score 7, sms.min_score 8
        (8, "high", ["push", "sms", "email"]),
        (9, "urgent", ["push", "sms", "email"]),
        (3, "normal", ["push"]),  # e.g. always-alert account, low score
        (None, "normal", ["push"]),
        (5, "urgent", ["push", "sms"]),  # urgent always gets SMS
    ],
)
async def test_channel_selection_by_score(score: int | None, priority: str, expected: list[str]) -> None:
    d, *_ = dispatcher()
    assert chans(await d.send(post_msg(priority=priority), score)) == expected


async def test_late_post_alerts_never_go_to_sms_or_pushover_high_priority(session) -> None:
    """Late (catch-up) alerts are old news: no SMS and no Pushover priority 1, whatever the score."""
    d, push, sms, email = dispatcher()
    late = AlertMessage(title="Missed during outage - posted 2h ago: x", body="b", priority="urgent", tags=["late"])
    assert chans(await d.send(late, 10)) == ["push", "email"]
    assert sms.sent == []
    po = PushoverNotifier(session, PO_APP, PO_USER, PushoverSettings(high_priority_min_score=8), timeout=4)
    assert po.priority(late, 10) == 0
    assert po.priority(AlertMessage(title="t", body="b", priority="low", tags=["late"]), 10) == -1


async def test_channel_selection_system_and_summary() -> None:
    d, *_ = dispatcher()
    assert chans(await d.send(post_msg(kind="system", priority="high"))) == ["push", "email"]
    assert chans(await d.send(post_msg(kind="system", priority="urgent"))) == ["push", "sms", "email"]
    assert chans(await d.send(post_msg(kind="summary", priority="low"))) == ["push", "email"]
    # summaries never go to SMS, even if marked urgent
    assert chans(await d.send(post_msg(kind="summary", priority="urgent"), 10)) == ["push", "email"]

    d2, *_ = dispatcher(email__send_daily_summary=False)
    assert chans(await d2.send(post_msg(kind="summary", priority="low"))) == ["push"]


async def test_channel_selection_custom_min_scores() -> None:
    d, *_ = dispatcher(sms__min_score=10, email__min_score=9)
    assert chans(await d.send(post_msg(), 8)) == ["push"]
    assert chans(await d.send(post_msg(), 9)) == ["push", "email"]


async def test_no_sms_or_email_notifier_configured() -> None:
    push = CaptureNotifier("push")
    d = AlertDispatcher(AlertSettings(), [push])
    assert chans(await d.send(post_msg(priority="urgent", kind="system"), 10)) == ["push"]
    assert d.channel_names == ["push"]


async def test_failing_channel_does_not_block_others(caplog: pytest.LogCaptureFixture) -> None:
    good = CaptureNotifier("ntfy")
    sms = CaptureNotifier("sms")
    d = AlertDispatcher(AlertSettings(), [RaisingNotifier(), good], sms=sms)  # type: ignore[list-item]
    with caplog.at_level(logging.WARNING):
        results = await d.send(post_msg(priority="urgent"), 9)
    assert chans(results) == ["broken", "ntfy", "sms"]
    assert [r.ok for r in results] == [False, True, True]
    assert results[0].error == "unexpected RuntimeError"
    assert len(good.sent) == 1 and len(sms.sent) == 1
    assert NTFY_TOKEN not in caplog.text


async def test_slow_channel_times_out_without_delaying_others() -> None:
    settings = AlertSettings(send_timeout_seconds=0.2)
    fast = SlowNotifier("fast", 0.0)
    slow = SlowNotifier("slow", 5.0)
    d = AlertDispatcher(settings, [slow, fast])  # type: ignore[list-item]
    loop = asyncio.get_running_loop()
    t0 = loop.time()
    results = await d.send(post_msg(), 8)
    assert loop.time() - t0 < 1.5
    by = {r.channel: r for r in results}
    assert by["fast"].ok and by["fast"].elapsed_ms < 150
    assert not by["slow"].ok and by["slow"].error == "timeout after 0.2s"
    assert by["slow"].elapsed_ms >= 150


async def test_email_gets_its_own_longer_timeout() -> None:
    """SEC-11: a slow but successful SMTP send is not reported as failed after the push timeout."""
    settings = AlertSettings(send_timeout_seconds=0.1)
    settings.email.timeout_seconds = 1.0
    push = SlowNotifier("ntfy", 0.0)
    email = SlowNotifier("email", 0.3)  # slower than the push timeout, within the email budget
    d = AlertDispatcher(settings, [push], email=email)  # type: ignore[list-item,arg-type]
    by = {r.channel: r for r in await d.send(post_msg(), 8)}
    assert by["ntfy"].ok and by["ntfy"].elapsed_ms < 100
    assert by["email"].ok and by["email"].elapsed_ms >= 250
    assert d.timeout_for(email) == 1.0 and d.timeout_for(push) == 0.1  # type: ignore[arg-type]

    settings.email.timeout_seconds = 0.1
    by = {r.channel: r for r in await d.send(post_msg(), 8)}
    assert not by["email"].ok and by["email"].error == "timeout after 0.1s (may still be delivered)"


async def test_channels_run_concurrently() -> None:
    a, b = SlowNotifier("a", 0.15), SlowNotifier("b", 0.15)
    d = AlertDispatcher(AlertSettings(), [a, b])  # type: ignore[list-item]
    loop = asyncio.get_running_loop()
    t0 = loop.time()
    results = await d.send(post_msg())
    assert all(r.ok for r in results)
    assert loop.time() - t0 < 0.28


async def test_dispatcher_passes_score_to_pushover(session) -> None:
    po = PushoverNotifier(session, PO_APP, PO_USER, PushoverSettings(high_priority_min_score=8), timeout=4)
    d = AlertDispatcher(AlertSettings(), [po])
    with aioresponses() as m:
        m.post(PUSHOVER_URL, status=200, payload={"status": 1})
        results = await d.send(post_msg(priority="high"), 8)
        kw = only_call(m, "POST", PUSHOVER_URL)
    assert results[0].ok
    assert kw["data"]["priority"] == "1"


async def test_dispatcher_with_no_channels_returns_empty() -> None:
    d = AlertDispatcher(AlertSettings(), [])
    assert await d.send(post_msg(), 9) == []
    assert d.channel_names == []


# =========================================================================== build_dispatcher


def app_config(settings: Settings, secrets: Secrets) -> AppConfig:
    wl = Watchlist(accounts=(), keyword_rules=(), my_tickers=(), company_names={})
    return AppConfig(settings, wl, secrets, Path("config/settings.yaml"), Path("config/watchlist.yaml"))


FULL_SECRETS = Secrets(
    ntfy_topic=NTFY_TOPIC,
    ntfy_token=NTFY_TOKEN,
    pushover_app_token=PO_APP,
    pushover_user_key=PO_USER,
    twilio_account_sid=TW_SID,
    twilio_auth_token=TW_TOKEN,
    twilio_from=TW_FROM,
    sms_to=TW_TO,
    smtp_host="smtp.example.com",
    smtp_username="me@example.com",
    smtp_password=SMTP_PASS,
    email_from="ceoscan@example.com",
    email_to="me@example.com",
)


async def test_build_dispatcher_all_channels(session) -> None:
    s = Settings()
    s.alerts.push = "both"
    s.alerts.sms.enabled = True
    s.alerts.email.enabled = True
    d = build_dispatcher(app_config(s, FULL_SECRETS), session)
    assert d.channel_names == ["ntfy", "pushover", "sms", "email"]
    assert isinstance(d.push[0], NtfyNotifier) and isinstance(d.push[1], PushoverNotifier)
    assert isinstance(d.sms, TwilioSmsNotifier) and isinstance(d.email, EmailNotifier)


async def test_build_dispatcher_email_uses_email_timeout(session) -> None:
    s = Settings()
    s.alerts.email.enabled = True
    s.alerts.email.timeout_seconds = 25.0
    d = build_dispatcher(app_config(s, FULL_SECRETS), session)
    assert isinstance(d.email, EmailNotifier) and d.email.timeout == 25.0  # smtplib per-operation timeout
    assert d.push[0].timeout == s.alerts.send_timeout_seconds  # type: ignore[attr-defined]
    assert d.timeout_for(d.email) == 25.0


async def test_build_dispatcher_default_is_ntfy_only(session) -> None:
    d = build_dispatcher(app_config(Settings(), FULL_SECRETS), session)
    assert d.channel_names == ["ntfy"]  # sms/email disabled by default


async def test_build_dispatcher_push_none(session) -> None:
    s = Settings()
    s.alerts.push = "none"
    assert build_dispatcher(app_config(s, FULL_SECRETS), session).push == []


async def test_build_dispatcher_missing_secrets_warns_and_skips(session, caplog: pytest.LogCaptureFixture) -> None:
    s = Settings()
    s.alerts.push = "both"
    s.alerts.sms.enabled = True
    s.alerts.email.enabled = True
    partial = Secrets(
        pushover_app_token=PO_APP,  # user key missing
        twilio_account_sid=TW_SID,
        twilio_auth_token=TW_TOKEN,  # from / to missing
        smtp_password=SMTP_PASS,  # host / from / to missing
    )
    with caplog.at_level(logging.WARNING, logger="ceoscan.alerts.dispatcher"):
        d = build_dispatcher(app_config(s, partial), session)
    assert d.channel_names == []
    assert d.sms is None and d.email is None
    text = caplog.text
    assert "NTFY_TOPIC" in text
    assert "PUSHOVER_USER_KEY" in text and "PUSHOVER_APP_TOKEN" not in text
    assert "TWILIO_FROM" in text and "ALERT_SMS_TO" in text
    assert "SMTP_HOST" in text and "ALERT_EMAIL_FROM" in text and "ALERT_EMAIL_TO" in text
    assert "no alert channels configured" in text
    assert_no_secrets(text)


async def test_build_dispatcher_keeps_working_channels(session, caplog: pytest.LogCaptureFixture) -> None:
    s = Settings()
    s.alerts.push = "both"
    sec = Secrets(ntfy_topic=NTFY_TOPIC)  # pushover not configured
    with caplog.at_level(logging.WARNING):
        d = build_dispatcher(app_config(s, sec), session)
    assert d.channel_names == ["ntfy"]
    assert "Pushover push disabled" in caplog.text
    assert_no_secrets(caplog.text)


# =========================================================================== log redaction (REQ-11 / SEC-7)


def _log_to_buffer(secrets: list[str]) -> tuple[logging.Logger, Any, Any]:
    import io

    from ceoscan.logging_setup import RedactingFilter, RedactingFormatter

    buf = io.StringIO()
    flt = RedactingFilter(secrets)
    handler = logging.StreamHandler(buf)
    handler.setFormatter(RedactingFormatter(flt))
    handler.addFilter(flt)
    log = logging.getLogger("ceoscan.test.redaction")
    log.handlers[:] = [handler]
    log.propagate = False
    log.setLevel(logging.DEBUG)
    return log, buf, handler


def test_tracebacks_and_exception_text_are_redacted() -> None:
    log, buf, _ = _log_to_buffer([TW_SID, NTFY_TOPIC])
    try:
        raise RuntimeError(f"failed calling https://api.twilio.com/2010-04-01/Accounts/{TW_SID}/Messages.json")
    except RuntimeError:
        log.exception("send failed for %s", NTFY_TOPIC)
    log.error("wrapped", exc_info=ValueError("Authorization: Bearer tk_supersecretvalue123"))
    log.warning("key sk-ant-api03-abcdefghijklmnop leaked", stack_info=True)
    text = buf.getvalue()
    assert TW_SID not in text and NTFY_TOPIC not in text
    assert "tk_supersecretvalue123" not in text and "abcdefghijklmnop" not in text
    assert "RuntimeError: failed calling https://api.twilio.com/2010-04-01/Accounts/***/Messages.json" in text
    assert "Traceback (most recent call last)" in text and "Bearer ***" in text and "sk-ant-***" in text
    assert "send failed for ***" in text


def test_redacting_formatter_does_not_leave_an_unredacted_cache_on_the_record() -> None:
    log, buf, handler = _log_to_buffer([SMTP_PASS])
    try:
        raise OSError(f"login failed with {SMTP_PASS}")
    except OSError:
        record = log.makeRecord(log.name, logging.ERROR, __file__, 1, "boom", None, sys_exc_info())
    handler.handle(record)
    assert SMTP_PASS not in buf.getvalue()
    assert record.exc_text is None or SMTP_PASS not in record.exc_text


def sys_exc_info() -> Any:
    import sys

    return sys.exc_info()


def test_setup_logging_installs_the_redacting_formatter(capsys: pytest.CaptureFixture[str]) -> None:
    from ceoscan.logging_setup import RedactingFormatter, setup_logging

    root = logging.getLogger()
    before, level = list(root.handlers), root.level
    try:
        setup_logging("INFO", [PO_APP])
        (handler,) = root.handlers
        assert isinstance(handler.formatter, RedactingFormatter)
        try:
            raise KeyError(PO_APP)
        except KeyError:
            logging.getLogger("ceoscan.test").exception("pushover failed")
        out = capsys.readouterr().out
        assert "pushover failed" in out and "KeyError: '***'" in out and PO_APP not in out
    finally:
        root.handlers[:] = before
        root.setLevel(level)
