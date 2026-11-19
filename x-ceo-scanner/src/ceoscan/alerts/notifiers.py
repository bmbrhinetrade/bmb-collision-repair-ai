"""Alert channels: ntfy, Pushover, Twilio SMS, email (SMTP), console and an in-memory capture.

Every notifier's ``send`` returns a ``ChannelResult`` and does not raise for expected failures (HTTP errors,
timeouts, connection problems, SMTP errors). ``ChannelResult.error`` never contains secrets: error text is
built from the status code and the provider's short error message, with every configured secret value
(tokens, keys, topic, phone numbers, SMTP password) scrubbed out. The ntfy topic is a secret (anyone who
knows it can read the alerts), which is one reason ntfy is published via the JSON API to the server root
rather than to ``{server}/{topic}``.
"""

from __future__ import annotations

import asyncio
import json
import logging
import smtplib
import ssl
import time
from email.message import EmailMessage
from email.utils import formatdate, make_msgid
from typing import Any, Callable, Iterable, Protocol, runtime_checkable

import aiohttp

from .. import USER_AGENT
from ..config import EmailSettings, PushoverSettings, Secrets
from ..models import AlertMessage, ChannelResult

logger = logging.getLogger(__name__)

PUSHOVER_URL = "https://api.pushover.net/1/messages.json"
TWILIO_URL = "https://api.twilio.com/2010-04-01/Accounts/{sid}/Messages.json"

PUSHOVER_TITLE_MAX = 250
PUSHOVER_MESSAGE_MAX = 1024
PUSHOVER_URL_MAX = 512
PUSHOVER_URL_TITLE_MAX = 100
TWILIO_BODY_MAX = 1600
ERROR_MAX = 300

NTFY_PRIORITY: dict[str, int] = {"low": 2, "normal": 3, "high": 4, "urgent": 5}
PUSHOVER_PRIORITY: dict[str, int] = {"low": -1, "normal": 0, "high": 0, "urgent": 1}


@runtime_checkable
class Notifier(Protocol):
    name: str

    async def send(self, msg: AlertMessage) -> ChannelResult: ...


# --------------------------------------------------------------------------- helpers


def _elapsed_ms(t0: float) -> int:
    return int((time.monotonic() - t0) * 1000)


def _clip(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return text[: max(limit - 1, 0)].rstrip() + "…"


class _Scrubber:
    """Replaces secret values in error strings with ``***``."""

    def __init__(self, values: Iterable[str | None]) -> None:
        self._values = sorted({v for v in values if v and len(v) >= 4}, key=len, reverse=True)

    def __call__(self, text: str) -> str:
        for v in self._values:
            if v in text:
                text = text.replace(v, "***")
        return text


def _one_line(text: str) -> str:
    """Collapse whitespace so provider messages fit on one log/result line."""
    return " ".join(str(text).split())


def _provider_detail(body_text: str) -> str:
    """Short, human readable error detail from a provider's JSON error body ('' if none)."""
    try:
        data: Any = json.loads(body_text)
    except (ValueError, TypeError):
        return ""
    if not isinstance(data, dict):
        return ""
    errs = data.get("errors")
    if isinstance(errs, list) and errs:  # Pushover: {"errors": ["user identifier is invalid"], ...}
        return "; ".join(str(e) for e in errs)
    message = data.get("error") or data.get("message") or data.get("detail") or data.get("title")
    if not message:
        return ""
    code = data.get("code")
    if code is not None and str(code) not in str(message):  # Twilio/ntfy numeric error codes
        return f"{message} (code {code})"
    return str(message)


async def _http_error(resp: aiohttp.ClientResponse, scrub: _Scrubber) -> str:
    try:
        text = await resp.text()
    except (aiohttp.ClientError, UnicodeDecodeError, ValueError):
        text = ""
    detail = _provider_detail(text) or (resp.reason or "")
    msg = f"HTTP {resp.status} {_one_line(detail)}".rstrip()
    return _clip(scrub(msg), ERROR_MAX)


def _exception_error(exc: BaseException, timeout: float, scrub: _Scrubber) -> str:
    if isinstance(exc, (asyncio.TimeoutError, TimeoutError)):
        return f"timeout after {timeout:g}s"
    detail = _one_line(str(exc))
    text = f"{type(exc).__name__}: {detail}" if detail else type(exc).__name__
    return _clip(scrub(text), ERROR_MAX)


async def _post(
    *,
    channel: str,
    session: aiohttp.ClientSession,
    url: str,
    timeout: float,
    scrub: _Scrubber,
    check_json: Callable[[Any], bool] | None = None,
    **kwargs: Any,
) -> ChannelResult:
    """POST and turn the outcome into a ChannelResult. Never raises for HTTP/network errors."""
    t0 = time.monotonic()
    headers = {"User-Agent": USER_AGENT, **kwargs.pop("headers", {})}
    try:
        async with session.post(
            url, headers=headers, timeout=aiohttp.ClientTimeout(total=timeout), **kwargs
        ) as resp:
            if not 200 <= resp.status < 300:
                err = await _http_error(resp, scrub)
                logger.debug("%s send failed: %s", channel, err)
                return ChannelResult(channel, False, _elapsed_ms(t0), status=resp.status, error=err)
            if check_json is not None:
                try:
                    data = await resp.json(content_type=None)
                except (ValueError, aiohttp.ClientError):
                    data = None
                if not check_json(data):
                    detail = _provider_detail(json.dumps(data)) if isinstance(data, dict) else ""
                    err = _clip(scrub(f"HTTP {resp.status} unexpected response {detail}".rstrip()), ERROR_MAX)
                    return ChannelResult(channel, False, _elapsed_ms(t0), status=resp.status, error=err)
            else:
                await resp.read()
            return ChannelResult(channel, True, _elapsed_ms(t0), status=resp.status)
    except (asyncio.TimeoutError, TimeoutError, aiohttp.ClientError, OSError, ValueError) as exc:
        err = _exception_error(exc, timeout, scrub)
        logger.debug("%s send failed: %s", channel, err)
        return ChannelResult(channel, False, _elapsed_ms(t0), error=err)


# --------------------------------------------------------------------------- ntfy


class NtfyNotifier:
    """ntfy push via the JSON publish API: POST {server}/ with {"topic", "title", "message", ...}.

    JSON publishing (instead of headers) keeps UTF-8/emoji titles intact and keeps the secret topic
    out of the request URL.
    """

    def __init__(
        self,
        session: aiohttp.ClientSession,
        server: str,
        topic: str,
        token: str | None = None,
        timeout: float = 5.0,
    ) -> None:
        self.name = "ntfy"
        self.session = session
        self.url = server.rstrip("/") + "/"
        self._topic = topic
        self._token = token
        self.timeout = float(timeout)
        self._scrub = _Scrubber([topic, token])

    def payload(self, msg: AlertMessage) -> dict[str, Any]:
        body: dict[str, Any] = {
            "topic": self._topic,
            "title": msg.title,
            "message": msg.body or msg.title,
            "priority": NTFY_PRIORITY.get(msg.priority, 3),
        }
        if msg.url:
            body["click"] = msg.url
        if msg.tags:
            body["tags"] = list(msg.tags)
        return body

    async def send(self, msg: AlertMessage) -> ChannelResult:
        headers = {"Authorization": f"Bearer {self._token}"} if self._token else {}
        return await _post(
            channel=self.name,
            session=self.session,
            url=self.url,
            timeout=self.timeout,
            scrub=self._scrub,
            json=self.payload(msg),
            headers=headers,
        )


# --------------------------------------------------------------------------- Pushover


class PushoverNotifier:
    """Pushover push: form POST to https://api.pushover.net/1/messages.json."""

    def __init__(
        self,
        session: aiohttp.ClientSession,
        app_token: str,
        user_key: str,
        settings: PushoverSettings,
        timeout: float = 5.0,
    ) -> None:
        self.name = "pushover"
        self.session = session
        self._app_token = app_token
        self._user_key = user_key
        self.settings = settings
        self.timeout = float(timeout)
        self._scrub = _Scrubber([app_token, user_key])

    def priority(self, msg: AlertMessage, score: int | None = None) -> int:
        """-1/0/0/1 for low/normal/high/urgent. Priority 1 (bypasses the phone's quiet hours) only for
        urgent messages; for post alerts with a known score, only at ``high_priority_min_score``+."""
        if msg.kind == "post" and score is not None:
            if score >= self.settings.high_priority_min_score:
                return 1
            return -1 if msg.priority == "low" else 0
        return PUSHOVER_PRIORITY.get(msg.priority, 0)

    def form(self, msg: AlertMessage, score: int | None = None) -> dict[str, str]:
        data = {
            "token": self._app_token,
            "user": self._user_key,
            "title": _clip(msg.title, PUSHOVER_TITLE_MAX),
            "message": _clip(msg.body or msg.title or "(empty)", PUSHOVER_MESSAGE_MAX),
            "priority": str(self.priority(msg, score)),
        }
        if msg.url and len(msg.url) <= PUSHOVER_URL_MAX:
            data["url"] = msg.url
            data["url_title"] = _clip("Open post" if msg.kind == "post" else "Open", PUSHOVER_URL_TITLE_MAX)
        if self.settings.sound:
            data["sound"] = self.settings.sound
        return data

    async def send(self, msg: AlertMessage, score: int | None = None) -> ChannelResult:
        return await _post(
            channel=self.name,
            session=self.session,
            url=PUSHOVER_URL,
            timeout=self.timeout,
            scrub=self._scrub,
            data=self.form(msg, score),
            check_json=lambda d: isinstance(d, dict) and d.get("status") == 1,
        )


# --------------------------------------------------------------------------- Twilio SMS


def _basic_auth_kwargs(login: str, password: str) -> dict[str, Any]:
    """HTTP basic auth request kwargs.

    aiohttp 3.14 deprecates ``aiohttp.BasicAuth`` in favour of ``aiohttp.encode_basic_auth()`` + an
    Authorization header; use that when available and fall back to ``BasicAuth`` on older aiohttp.
    """
    encode = getattr(aiohttp, "encode_basic_auth", None)
    if encode is not None:
        return {"headers": {"Authorization": encode(login, password)}}
    return {"auth": aiohttp.BasicAuth(login, password)}  # pragma: no cover - aiohttp < 3.14


class TwilioSmsNotifier:
    """SMS via Twilio's Messages API (form-encoded, HTTP basic auth with account SID + auth token)."""

    def __init__(
        self,
        session: aiohttp.ClientSession,
        account_sid: str,
        auth_token: str,
        from_number: str,
        to_number: str,
        timeout: float = 5.0,
    ) -> None:
        self.name = "sms"
        self.session = session
        self._sid = account_sid
        self._auth_kwargs = _basic_auth_kwargs(account_sid, auth_token)
        self._from = from_number
        self._to = to_number
        self.timeout = float(timeout)
        self._scrub = _Scrubber([account_sid, auth_token, from_number, to_number])

    @property
    def url(self) -> str:
        return TWILIO_URL.format(sid=self._sid)

    async def send(self, msg: AlertMessage) -> ChannelResult:
        body = _clip(msg.sms_text or msg.title, TWILIO_BODY_MAX)
        return await _post(
            channel=self.name,
            session=self.session,
            url=self.url,
            timeout=self.timeout,
            scrub=self._scrub,
            data={"To": self._to, "From": self._from, "Body": body},
            **self._auth_kwargs,
        )


# --------------------------------------------------------------------------- email


class EmailNotifier:
    """Email via stdlib smtplib, run in a worker thread. STARTTLS by default, SMTPS when use_ssl."""

    def __init__(self, secrets: Secrets, settings: EmailSettings, timeout: float = 10.0) -> None:
        self.name = "email"
        self._secrets = secrets
        self.settings = settings
        self.timeout = float(timeout)
        self._scrub = _Scrubber([secrets.smtp_password, secrets.smtp_username])

    @property
    def port(self) -> int:
        port = int(self._secrets.smtp_port or (465 if self.settings.use_ssl else 587))
        if self.settings.use_ssl and port == 587:  # 587 is the STARTTLS submission port; SMTPS lives on 465
            return 465
        return port

    def build_message(self, msg: AlertMessage) -> EmailMessage:
        s = self._secrets
        em = EmailMessage()
        em["Subject"] = _one_line(msg.title)[:250] or "ceoscan alert"
        em["From"] = s.email_from or ""
        em["To"] = s.email_to or ""
        em["Date"] = formatdate(usegmt=True)
        domain = (s.email_from or "").rpartition("@")[2].strip(" >") or "ceoscan.local"
        em["Message-ID"] = make_msgid(domain=domain)
        body = msg.body or msg.title
        if msg.url and msg.url not in body:
            body = f"{body}\n\n{msg.url}"
        em.set_content(body + "\n\n-- \nceoscan (alerts only; never trades)\n")
        return em

    def _send_sync(self, em: EmailMessage) -> None:
        s = self._secrets
        host = s.smtp_host or ""
        ctx = ssl.create_default_context()
        smtp: smtplib.SMTP
        if self.settings.use_ssl:
            smtp = smtplib.SMTP_SSL(host, self.port, timeout=self.timeout, context=ctx)
        else:
            smtp = smtplib.SMTP(host, self.port, timeout=self.timeout)
        with smtp:
            if not self.settings.use_ssl:
                smtp.ehlo()
                smtp.starttls(context=ctx)
                smtp.ehlo()
            if s.smtp_username and s.smtp_password:
                smtp.login(s.smtp_username, s.smtp_password)
            smtp.send_message(em)

    async def send(self, msg: AlertMessage) -> ChannelResult:
        t0 = time.monotonic()
        try:
            em = self.build_message(msg)
            await asyncio.to_thread(self._send_sync, em)
        except smtplib.SMTPResponseException as exc:
            raw = exc.smtp_error
            detail = raw.decode("utf-8", "replace") if isinstance(raw, bytes) else str(raw)
            err = _clip(self._scrub(f"{type(exc).__name__} {exc.smtp_code}: {_one_line(detail)}"), ERROR_MAX)
            return ChannelResult(self.name, False, _elapsed_ms(t0), status=int(exc.smtp_code), error=err)
        except (smtplib.SMTPException, OSError, ValueError, TypeError) as exc:
            err = _exception_error(exc, self.timeout, self._scrub)
            return ChannelResult(self.name, False, _elapsed_ms(t0), error=err)
        return ChannelResult(self.name, True, _elapsed_ms(t0))


# --------------------------------------------------------------------------- dev / test


class ConsoleNotifier:
    """Prints alerts to stdout (replay / development)."""

    def __init__(self, name: str = "console", out: Callable[[str], Any] = print) -> None:
        self.name = name
        self._out = out

    async def send(self, msg: AlertMessage) -> ChannelResult:
        t0 = time.monotonic()
        rule = "-" * 60
        self._out(f"{rule}\n[{msg.kind.upper()} {msg.priority}] {msg.title}\n{msg.body}\n{rule}")
        return ChannelResult(self.name, True, _elapsed_ms(t0))


class CaptureNotifier:
    """Keeps every message in ``.sent`` (tests). ``ok=False`` simulates a failing channel."""

    def __init__(self, name: str = "capture", ok: bool = True) -> None:
        self.name = name
        self.ok = ok
        self.sent: list[AlertMessage] = []

    async def send(self, msg: AlertMessage) -> ChannelResult:
        self.sent.append(msg)
        if self.ok:
            return ChannelResult(self.name, True, 0)
        return ChannelResult(self.name, False, 0, error="simulated failure")
