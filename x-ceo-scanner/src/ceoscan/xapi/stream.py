"""X API v2 filtered-stream consumer.

``FilteredStream.run`` keeps one long-lived connection to ``GET /2/tweets/search/stream``
open, splits the chunked body into JSON lines and puts every post on the pipeline queue as
an :class:`~ceoscan.models.IngestItem`.

Robustness features (all required for an always-on alerting service):

* exponential / linear backoff with jitter following X's reconnect guidance (:class:`Backoff`),
  honouring ``x-rate-limit-reset`` on HTTP 429;
* an explicit heartbeat watchdog task: X sends ``\\r\\n`` keep-alives every ~20 s, so if no
  bytes arrive for ``stream.heartbeat_timeout_seconds`` the read is cancelled and we reconnect;
* the body is read with ``resp.content.iter_any()`` plus :class:`LineBuffer`, so arbitrarily
  large lines are fine (no ``readline`` size limits);
* detection of "this plan has no filtered-stream access" (HTTP 403) via
  :class:`StreamUnavailable`, so the app can fall back to timeline polling;
* gap recovery: after reconnecting from an outage of at least ``recovery.min_gap_seconds`` (default 0,
  i.e. every reconnect; the gap is measured from the last bytes received minus X's ~5 s delivery lag) the
  ``on_reconnect_gap`` callback (timeline polling with ``since_id``) runs in the background.
  ``on_gap_start`` fires once when an outage begins, so the caller can snapshot its cursors before
  live posts move them. With ``catch_up_on_first_connect`` the first connection of the process is
  treated as the end of a gap too (posts made while ceoscan was restarting or deployed);
* ingest errors (disconnects, HTTP errors) are written to the store's events table through
  :class:`IngestEventLog`, at most one per kind per minute, so the daily summary counts them.

The bearer token is only ever placed in the request headers; nothing here logs headers,
tokens or URLs.
"""

from __future__ import annotations

import asyncio
import json
import logging
import random
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Literal

import aiohttp

from .. import USER_AGENT
from ..config import XApiSettings
from ..models import IngestItem, IngestStatus
from ..timeutil import now_utc
from .client import (
    CreditsDepleted,
    NotAuthorized,
    RateLimited,
    RateLimitInfo,
    TooManyConnections,
    XApiError,
    XClient,
    error_from_response,
)

log = logging.getLogger(__name__)

STREAM_PATH = "/2/tweets/search/stream"

#: A connection that stayed up this long (or delivered a post) counts as healthy -> Backoff.reset().
HEALTHY_CONNECTION_SECONDS = 30.0
#: Retry delay after terminating our own stale connection on TooManyConnections.
TOO_MANY_CONNECTIONS_RETRY_SECONDS = 5.0
#: After this many consecutive TooManyConnections answers, fall back to rate-limit backoff.
TOO_MANY_CONNECTIONS_FAST_RETRIES = 3
#: Generic HTTP 403s in a row before we conclude the plan has no stream access.
MAX_CONSECUTIVE_FORBIDDEN = 3
#: Error bodies are tiny; never wait long for one.
ERROR_BODY_TIMEOUT_SECONDS = 5.0
#: X delivers filtered-stream posts up to ~4-5 s (P99) after they were created, so posts created
#: shortly before the last bytes we saw may never have arrived. Added to the measured gap.
STREAM_DELIVERY_MARGIN_SECONDS = 5.0
#: The heartbeat watchdog re-evaluates its deadline at least this often (the idle limit shrinks
#: from connect_timeout + heartbeat to heartbeat once the response headers arrive).
WATCHDOG_MAX_SLEEP_SECONDS = 1.0
#: Ingest error events (store.events) are written at most once per kind per this many seconds.
ERROR_EVENT_INTERVAL_SECONDS = 60.0
_MAX_ERROR_TEXT = 300

BackoffKind = Literal["network", "http", "rate_limit"]


class StreamUnavailable(Exception):
    """The app/plan has no filtered-stream access (e.g. HTTP 403 client-not-enrolled)."""


# --------------------------------------------------------------------------- error events


class IngestEventLog:
    """Write ingest errors to ``store.log_event`` (the daily summary counts them), throttled.

    At most one event per ``kind`` per ``interval`` seconds is written; repeats in between are
    counted and mentioned in the next event of that kind ("+N more"). Never raises: bookkeeping
    must not break ingest. ``detail`` must not contain secrets (callers pass X problem titles /
    exception summaries only).
    """

    def __init__(
        self,
        store: Any,
        interval: float = ERROR_EVENT_INTERVAL_SECONDS,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.store = store
        self.interval = float(interval)
        self._clock = clock
        self._last: dict[str, float] = {}
        self._suppressed: dict[str, int] = {}

    def error(self, kind: str, detail: str, level: str = "error") -> bool:
        """Record one error of ``kind``. Returns True if an event row was written."""
        if self.store is None:
            return False
        now = self._clock()
        last = self._last.get(kind)
        if last is not None and now - last < self.interval:
            self._suppressed[kind] = self._suppressed.get(kind, 0) + 1
            return False
        self._last[kind] = now
        more = self._suppressed.pop(kind, 0)
        text = " ".join(str(detail).split())[:_MAX_ERROR_TEXT]
        if more:
            text += f" (+{more} more since the previous {kind} event)"
        try:
            self.store.log_event(kind=kind, detail=text, level=level)
        except Exception:  # never let bookkeeping break ingest
            log.debug("could not record a %s event", kind, exc_info=True)
            return False
        return True


# --------------------------------------------------------------------------- backoff


class Backoff:
    """Reconnect delays per X's guidance, with jitter.

    * ``network`` (TCP/TLS errors, dropped or stalled streams): 0 s for the first attempt
      after a drop, then +0.25 s per attempt, capped at 16 s.
    * ``http`` (HTTP errors): 5, 10, 20, ... capped at 320 s.
    * ``rate_limit`` (HTTP 429): 60, 120, 240, ... capped at 960 s, and never earlier than
      ``reset_at`` (the ``x-rate-limit-reset`` time).

    Each kind escalates independently; :meth:`reset` (called after a healthy connection)
    starts them all over. Every delay is multiplied by ``uniform(1 - jitter, 1 + jitter)``;
    the ``reset_at`` floor is applied after jitter.
    """

    NETWORK_STEP = 0.25
    NETWORK_CAP = 16.0
    HTTP_BASE = 5.0
    HTTP_CAP = 320.0
    RATE_LIMIT_BASE = 60.0
    RATE_LIMIT_CAP = 960.0

    def __init__(self, jitter: float = 0.2, rng: random.Random | None = None) -> None:
        if not 0.0 <= jitter < 1.0:
            raise ValueError("jitter must be in [0, 1)")
        self.jitter = jitter
        self._rng = rng or random.Random()
        self._attempts: dict[str, int] = {"network": 0, "http": 0, "rate_limit": 0}

    def attempts(self, kind: BackoffKind) -> int:
        """Number of delays handed out for ``kind`` since the last reset."""
        return self._attempts[kind]

    @classmethod
    def base_delay(cls, kind: BackoffKind, attempt: int) -> float:
        """Un-jittered delay for the ``attempt``-th (1-based) retry of ``kind``."""
        n = max(1, attempt)
        exp = min(n - 1, 30)  # keep the arithmetic small; the caps kick in long before this
        if kind == "network":
            return min(cls.NETWORK_STEP * (n - 1), cls.NETWORK_CAP)
        if kind == "http":
            return min(cls.HTTP_BASE * (2**exp), cls.HTTP_CAP)
        if kind == "rate_limit":
            return min(cls.RATE_LIMIT_BASE * (2**exp), cls.RATE_LIMIT_CAP)
        raise ValueError(f"unknown backoff kind {kind!r}")

    def jittered(self, seconds: float) -> float:
        """``seconds`` multiplied by the jitter factor, never below 0."""
        if seconds <= 0:
            return 0.0
        return max(0.0, seconds * self._rng.uniform(1.0 - self.jitter, 1.0 + self.jitter))

    def next_delay(
        self,
        kind: BackoffKind,
        reset_at: datetime | None = None,
        now: datetime | None = None,
    ) -> float:
        if kind not in self._attempts:
            raise ValueError(f"unknown backoff kind {kind!r}")
        self._attempts[kind] += 1
        delay = self.jittered(self.base_delay(kind, self._attempts[kind]))
        if reset_at is not None:
            until_reset = (reset_at - (now or now_utc())).total_seconds()
            delay = max(delay, until_reset)
        return max(0.0, delay)

    def reset(self) -> None:
        for kind in self._attempts:
            self._attempts[kind] = 0


# --------------------------------------------------------------------------- line splitting


class LineBuffer:
    """Split a chunked byte stream into lines.

    ``feed`` returns the complete lines contained in the bytes seen so far, with the
    ``\\r\\n`` / ``\\n`` terminator stripped. A keep-alive (``\\r\\n`` on its own) yields
    ``b""``. Incomplete trailing data is kept until its newline arrives; there is no
    line-length limit (long posts with expansions can be large).
    """

    def __init__(self) -> None:
        self._buf = bytearray()

    @property
    def pending(self) -> int:
        """Bytes buffered without a terminating newline yet."""
        return len(self._buf)

    def feed(self, chunk: bytes) -> list[bytes]:
        if not chunk:
            return []
        self._buf += chunk
        if b"\n" not in chunk:  # fast path: a big line arriving in many chunks
            return []
        *lines, rest = bytes(self._buf).split(b"\n")
        self._buf = bytearray(rest)
        return [line[:-1] if line.endswith(b"\r") else line for line in lines]


# --------------------------------------------------------------------------- helpers


def stream_params(settings: XApiSettings) -> dict[str, str]:
    """Query parameters for the stream connection (fields/expansions, optional backfill)."""
    params: dict[str, str] = {}
    for key, values in (
        ("tweet.fields", settings.tweet_fields),
        ("expansions", settings.expansions),
        ("user.fields", settings.user_fields),
    ):
        joined = ",".join(dict.fromkeys(v.strip() for v in values if v and v.strip()))
        if joined:
            params[key] = joined
    if settings.stream.backfill_minutes > 0:
        params["backfill_minutes"] = str(int(settings.stream.backfill_minutes))
    return params


def stream_url(settings: XApiSettings) -> str:
    return settings.base_url.rstrip("/") + STREAM_PATH


def stream_headers(bearer_token: str) -> dict[str, str]:
    # identity encoding: a compressed body can hold keep-alives back in the decompressor,
    # which would defeat the heartbeat watchdog. Volume is tiny, so compression buys nothing.
    return {
        "Authorization": f"Bearer {bearer_token}",
        "User-Agent": USER_AGENT,
        "Accept-Encoding": "identity",
    }


_NO_ACCESS_MARKERS = (
    "client-not-enrolled",
    "client-forbidden",
    "client forbidden",
    "required_enrollment",
    "appropriate level of api access",
    "not enrolled",
)


def indicates_no_stream_access(err: XApiError) -> bool:
    """True when a 403 says this app/plan cannot use the filtered stream at all."""
    if err.status != 403:
        return False
    parts: list[str] = [err.title or "", err.detail or "", err.problem_type or ""]
    body = getattr(err, "body", None)  # parsed problem document, when the client kept it
    if body:
        try:
            parts.append(json.dumps(body, default=str))
        except (TypeError, ValueError):  # pragma: no cover - X bodies are plain JSON
            pass
    text = " ".join(parts).lower()
    return any(marker in text for marker in _NO_ACCESS_MARKERS)


StreamErrorKind = Literal["disconnect", "credits", "too_many_connections", "other"]


def classify_stream_errors(errors: Any) -> StreamErrorKind:
    """Classify an in-stream ``{"errors": [...]}`` line."""
    if not isinstance(errors, list):
        return "other"
    kind: StreamErrorKind = "other"
    for e in errors:
        if not isinstance(e, dict):
            continue
        title = str(e.get("title") or "").lower()
        ptype = str(e.get("type") or "").lower()
        if "credits" in ptype or title.replace(" ", "") == "creditsdepleted":
            return "credits"
        if e.get("connection_issue") == "TooManyConnections":
            kind = "too_many_connections"
        elif "disconnect_type" in e or "operational-disconnect" in title or "operational-disconnect" in ptype:
            if kind == "other":
                kind = "disconnect"
    return kind


def summarize_stream_errors(errors: Any) -> str:
    """Short human readable summary of X problem objects (titles/details only)."""
    if not isinstance(errors, list):
        return "unknown stream error"
    parts: list[str] = []
    for e in errors[:3]:
        if isinstance(e, dict):
            title = str(e.get("title") or e.get("disconnect_type") or "error")
            detail = str(e.get("detail") or e.get("message") or "")
            parts.append(f"{title}: {detail}" if detail else title)
    text = "; ".join(parts) or "unknown stream error"
    return " ".join(text.split())[:_MAX_ERROR_TEXT]


def _describe_exc(exc: BaseException) -> str:
    text = str(exc).strip()
    desc = f"{type(exc).__name__}: {text}" if text else type(exc).__name__
    return " ".join(desc.split())[:_MAX_ERROR_TEXT]


async def _wait_or_stop(stop: asyncio.Event, seconds: float) -> bool:
    """Sleep up to ``seconds``; return True as soon as ``stop`` is set."""
    if stop.is_set():
        return True
    if seconds <= 0:
        return False
    try:
        await asyncio.wait_for(stop.wait(), timeout=seconds)
    except TimeoutError:
        return False
    return True


async def read_error_body(resp: aiohttp.ClientResponse) -> dict[str, Any] | str | None:
    """Read a (small) non-200 body without hanging; JSON object if possible, else text."""
    try:
        raw = await asyncio.wait_for(resp.read(), timeout=ERROR_BODY_TIMEOUT_SECONDS)
    except (aiohttp.ClientError, OSError, TimeoutError):
        return None
    text = raw.decode("utf-8", errors="replace").strip()
    if not text:
        return None
    try:
        parsed = json.loads(text)
    except ValueError:
        return text[:2000]
    return parsed if isinstance(parsed, dict) else text[:2000]


@dataclass
class _StreamEnd:
    """Why a 200 stream stopped delivering."""

    kind: Literal["closed", "disconnect", "credits", "too_many_connections"]
    detail: str


# (delay seconds before the next attempt, status.state while waiting)
_Retry = tuple[float, str]


# --------------------------------------------------------------------------- stream


class FilteredStream:
    """Long-lived filtered-stream reader. See the module docstring for behaviour."""

    def __init__(
        self,
        session: aiohttp.ClientSession,
        bearer_token: str,
        settings: XApiSettings,
        queue: asyncio.Queue,
        status: IngestStatus,
        client: XClient | None = None,
        on_reconnect_gap: Callable[[float], Awaitable[None]] | None = None,
        backoff: Backoff | None = None,
        *,
        on_gap_start: Callable[[], None] | None = None,
        on_connected: Callable[[], None] | None = None,
        catch_up_on_first_connect: bool = False,
        gap_started_at: datetime | None = None,
        events: IngestEventLog | None = None,
    ) -> None:
        """``on_gap_start``: called (synchronously) once when an outage begins - snapshot cursors here.
        ``on_connected``: called on every successful (HTTP 200) connection.
        ``catch_up_on_first_connect``: run ``on_reconnect_gap`` after the first connection too
        (whatever the gap), so posts made while the process was not running are fetched.
        ``gap_started_at``: when coverage ended before this process started (e.g. a persisted
        last-alive time); only used to report the startup gap.
        ``events``: where disconnects / HTTP errors are recorded (``stream_error`` events).
        """
        if not bearer_token:
            raise ValueError("an X bearer token is required")
        self._session = session
        self._token = bearer_token
        self.settings = settings
        self.queue = queue
        self.status = status
        self.client = client
        self.on_reconnect_gap = on_reconnect_gap
        self.on_gap_start = on_gap_start
        self.on_connected = on_connected
        self.catch_up_on_first_connect = catch_up_on_first_connect
        self.events = events
        self.backoff = backoff or Backoff()
        self.url = stream_url(settings)
        self.params = stream_params(settings)
        # total=None: the stream is meant to stay open forever; the watchdog covers stalls.
        self.timeout = aiohttp.ClientTimeout(
            total=None,
            connect=None,
            sock_connect=settings.stream.connect_timeout_seconds,
            sock_read=None,
        )
        # counters (observability / tests)
        self.connections = 0  # successful HTTP 200 connections
        self.posts_received = 0
        # cross-attempt state
        self._ever_connected = False
        self._gap_started_at: datetime | None = gap_started_at
        self._forbidden_streak = 0
        self._tmc_streak = 0
        self._background: set[asyncio.Task[None]] = set()
        # per-attempt state (reset in _attempt)
        self._last_activity = 0.0  # loop.time() of the last bytes / progress
        self._idle_limit = 0.0
        self._watchdog_fired = False
        self._paused = False  # True while blocked on a full pipeline queue
        self._conn_open = False
        self._conn_started = 0.0
        self._healthy = False
        self._got_data = False
        self._last_bytes_at: datetime | None = None

    def __repr__(self) -> str:  # never show the token
        return f"FilteredStream(url={self.url!r}, state={self.status.state!r})"

    # ------------------------------------------------------------------ main loop

    async def run(self, stop: asyncio.Event) -> None:
        """Connect and read until ``stop`` is set. Raises :class:`StreamUnavailable`."""
        self.status.mode = "stream"
        unavailable = False
        try:
            while not stop.is_set():
                retry = await self._attempt(stop)
                if retry is None or stop.is_set():
                    break
                delay, state = retry
                if delay > 0:
                    self.status.state = state
                    log.info("stream: next connection attempt in %.1fs (%s)", delay, state)
                    if await _wait_or_stop(stop, delay):
                        break
                if self.status.state != "credits_depleted":  # keep the cause visible while retrying
                    self.status.state = "reconnecting"
        except StreamUnavailable:
            unavailable = True
            raise
        finally:
            for task in list(self._background):
                task.cancel()
            if self._background:
                await asyncio.gather(*self._background, return_exceptions=True)
            self.status.connected_since = None
            if not unavailable:
                self.status.state = "stopped"
            log.info("stream: stopped (%d connections, %d posts)", self.connections, self.posts_received)

    async def _attempt(self, stop: asyncio.Event) -> _Retry | None:
        """One connection attempt guarded by the heartbeat watchdog.

        Returns the retry delay/state, or None when ``stop`` ended the attempt.
        """
        loop = asyncio.get_running_loop()
        hb = self.settings.stream.heartbeat_timeout_seconds
        self._watchdog_fired = False
        self._paused = False
        self._conn_open = False
        self._healthy = False
        self._got_data = False
        self._last_activity = loop.time()
        # Until response headers arrive allow the connect timeout plus one heartbeat period.
        self._idle_limit = self.settings.stream.connect_timeout_seconds + hb

        reader = asyncio.create_task(self._connect_and_read(), name="ceoscan-stream-reader")
        watchdog = asyncio.create_task(self._watchdog(reader), name="ceoscan-stream-watchdog")
        stopper = asyncio.create_task(stop.wait(), name="ceoscan-stream-stop")
        try:
            await asyncio.wait({reader, stopper}, return_when=asyncio.FIRST_COMPLETED)
        finally:
            watchdog.cancel()
            stopper.cancel()
            if not reader.done():
                reader.cancel()
            await asyncio.gather(reader, watchdog, stopper, return_exceptions=True)

        if reader.cancelled():
            if self._watchdog_fired and not stop.is_set():
                return self._on_heartbeat_timeout()
            self._conn_open = False
            return None
        return reader.result()  # re-raises StreamUnavailable

    async def _watchdog(self, reader: asyncio.Task[Any]) -> None:
        """Cancel ``reader`` when no bytes arrived for ``self._idle_limit`` seconds."""
        loop = asyncio.get_running_loop()
        while not reader.done():
            now = loop.time()
            deadline = self._last_activity + self._idle_limit
            if self._paused:
                await asyncio.sleep(0.25)
                continue
            if now < deadline:
                # Capped: _connect_and_read shrinks _idle_limit once the headers arrive, and a
                # long sleep toward the old (connect + heartbeat) deadline would miss that.
                await asyncio.sleep(min(max(deadline - now, 0.01), WATCHDOG_MAX_SLEEP_SECONDS))
                continue
            self._watchdog_fired = True
            reader.cancel()
            return

    def _touch(self) -> None:
        self._last_activity = asyncio.get_running_loop().time()

    # ------------------------------------------------------------------ connection

    async def _connect_and_read(self) -> _Retry:
        try:
            async with self._session.get(
                self.url,
                params=self.params,
                headers=stream_headers(self._token),
                timeout=self.timeout,
            ) as resp:
                self._touch()
                if resp.status != 200:
                    body = await read_error_body(resp)
                    err = error_from_response(resp.status, body, resp.headers)
                    return await self._on_http_error(err, resp.headers)
                self._idle_limit = self.settings.stream.heartbeat_timeout_seconds
                self._on_connected()
                end = await self._read_stream(resp)
            return await self._on_stream_end(end)
        except StreamUnavailable:
            raise
        except (aiohttp.ClientError, OSError, TimeoutError) as exc:
            return self._on_network_error(exc)
        except Exception as exc:  # a bug must not kill the always-on stream
            log.exception("stream: unexpected error")
            self._mark_down(_describe_exc(exc))
            return self.backoff.next_delay("http"), "backoff"

    async def _read_stream(self, resp: aiohttp.ClientResponse) -> _StreamEnd:
        buf = LineBuffer()
        async for chunk in resp.content.iter_any():
            received_at = self._on_bytes()
            for line in buf.feed(chunk):
                if not line.strip():
                    continue  # keep-alive
                end = await self._handle_line(line, received_at)
                if end is not None:
                    return end
            self._check_healthy()
        return _StreamEnd("closed", "stream closed by server")

    def _on_bytes(self) -> datetime:
        self._touch()
        now = now_utc()
        self.status.last_heartbeat_at = now
        self._last_bytes_at = now
        return now

    async def _handle_line(self, line: bytes, received_at: datetime) -> _StreamEnd | None:
        try:
            obj = json.loads(line)
        except ValueError:
            log.warning("stream: skipped a line that is not valid JSON (%d bytes)", len(line))
            return None
        if not isinstance(obj, dict):
            return None
        if obj.get("data"):
            await self._enqueue(IngestItem(payload=obj, received_at=received_at, source="stream"))
            self.posts_received += 1
            self._got_data = True
            if obj.get("errors"):
                log.debug("stream: post with partial errors: %s", summarize_stream_errors(obj["errors"]))
            return None
        errors = obj.get("errors")
        if not errors:
            return None
        summary = summarize_stream_errors(errors)
        kind = classify_stream_errors(errors)
        if kind == "other":
            log.warning("stream: X reported: %s", summary)
            return None
        log.debug("stream: X ended the stream (%s): %s", kind, summary)  # logged by _on_stream_end
        return _StreamEnd(kind, summary)

    async def _enqueue(self, item: IngestItem) -> None:
        try:
            self.queue.put_nowait(item)
            return
        except asyncio.QueueFull:
            log.warning("stream: pipeline queue full (%d items); pausing reads", self.queue.qsize())
        self._paused = True  # we are not reading, so silence is not the network's fault
        try:
            await self.queue.put(item)
        finally:
            self._paused = False
            self._touch()

    def _check_healthy(self) -> None:
        if self._healthy or not self._conn_open:
            return
        up_for = asyncio.get_running_loop().time() - self._conn_started
        if self._got_data or up_for >= HEALTHY_CONNECTION_SECONDS:
            self._healthy = True
            self.backoff.reset()

    # ------------------------------------------------------------------ status transitions

    def _on_connected(self) -> None:
        now = now_utc()
        st = self.status
        first = not self._ever_connected
        if self._ever_connected:
            st.reconnects += 1
        self._ever_connected = True
        self.connections += 1
        self._conn_open = True
        self._conn_started = asyncio.get_running_loop().time()
        self._last_bytes_at = now
        self._forbidden_streak = 0
        self._tmc_streak = 0
        st.mode = "stream"
        st.state = "connected"
        st.connected_since = now
        st.last_heartbeat_at = now
        st.down_since = None
        if self.on_connected is not None:
            try:
                self.on_connected()
            except Exception:  # a callback bug must not drop the connection
                log.exception("stream: on_connected callback failed")
        gap_start, self._gap_started_at = self._gap_started_at, None
        startup = first and self.catch_up_on_first_connect
        if gap_start is None and not startup:
            log.info("stream: connected")
            return
        gap = max(0.0, (now - gap_start).total_seconds()) if gap_start is not None else 0.0
        if startup:
            log.info(
                "stream: connected; catching up on posts made while ceoscan was not connected%s",
                f" ({gap:.0f}s)" if gap_start is not None else "",
            )
        else:
            log.info("stream: connected after %.1fs without coverage", gap)
        rec = self.settings.recovery
        # Posts created up to X's delivery lag before the last bytes may never have arrived.
        effective_gap = gap + STREAM_DELIVERY_MARGIN_SECONDS
        if self.on_reconnect_gap is not None and rec.enabled and (startup or effective_gap >= rec.min_gap_seconds):
            task = asyncio.create_task(self._run_gap_recovery(gap), name="ceoscan-gap-recovery")
            self._background.add(task)
            task.add_done_callback(self._background.discard)

    async def _run_gap_recovery(self, gap: float) -> None:
        assert self.on_reconnect_gap is not None
        try:
            await self.on_reconnect_gap(gap)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("stream: gap recovery failed")

    def _mark_down(self, error: str | None) -> None:
        """Record that we are not connected (keeps the first down_since while down)."""
        now = now_utc()
        st = self.status
        if self._conn_open:
            self._check_healthy()
            # Coverage really ended when the last bytes arrived (matters for heartbeat stalls).
            gap_start = self._last_bytes_at or now
        else:
            gap_start = now
        self._conn_open = False
        if st.down_since is None:
            st.down_since = now
        if self._gap_started_at is None:
            self._gap_started_at = gap_start
            if self.on_gap_start is not None:
                try:  # e.g. snapshot the since_id cursors before live posts can move them
                    self.on_gap_start()
                except Exception:
                    log.exception("stream: on_gap_start callback failed")
        st.connected_since = None
        if st.state == "connected":
            st.state = "reconnecting"
        if error:
            st.last_error = error[:_MAX_ERROR_TEXT]
            if self.events is not None:
                self.events.error("stream_error", error)

    def _on_network_error(self, exc: BaseException) -> _Retry:
        msg = _describe_exc(exc)
        self._mark_down(msg)
        delay = self.backoff.next_delay("network")
        log.warning("stream: network error: %s", msg)
        return delay, "backoff"

    def _on_heartbeat_timeout(self) -> _Retry:
        if self._conn_open:
            hb = self.settings.stream.heartbeat_timeout_seconds
            msg = f"no data or keep-alive for {hb:g}s (heartbeat timeout)"
        else:
            msg = "timed out waiting for the stream response"
        self._mark_down(msg)
        log.warning("stream: %s; reconnecting", msg)
        return self.backoff.next_delay("network"), "backoff"

    async def _on_stream_end(self, end: _StreamEnd) -> _Retry:
        if end.kind == "credits":
            return self._credits_depleted(f"credits depleted: {end.detail}")
        if end.kind == "too_many_connections":
            self._mark_down(f"too many connections: {end.detail}")
            return await self._too_many_connections(None)
        self._mark_down(end.detail if end.kind == "closed" else f"disconnected by X: {end.detail}")
        log.warning("stream: %s; reconnecting", self.status.last_error)
        return self.backoff.next_delay("network"), "backoff"

    def _credits_depleted(self, msg: str) -> _Retry:
        self._mark_down(msg)
        self.status.state = "credits_depleted"
        retry = self.settings.stream.credits_depleted_retry_seconds
        log.error(
            "stream: X API credits depleted - add credits at console.x.com; retrying in %.0fs", retry
        )
        return retry, "credits_depleted"

    async def _too_many_connections(self, rate_limit: RateLimitInfo | None) -> _Retry:
        self._tmc_streak += 1
        # Wait for the connect window's reset only when the connect budget is really used up;
        # with connects left, a stale connection on X's side is the cause - plain backoff.
        exhausted = rate_limit is not None and rate_limit.remaining is not None and rate_limit.remaining <= 0
        reset_at = rate_limit.reset_at if exhausted and rate_limit is not None else None
        if self.settings.stream.terminate_stale_connections and self.client is not None:
            log.warning("stream: TooManyConnections; terminating our stale connection(s)")
            self._touch()
            try:
                await self.client.terminate_stream_connections()
            except Exception as exc:  # the client already logs; never let this kill the loop
                log.warning("stream: terminating stale connections failed: %s", _describe_exc(exc))
            if self._tmc_streak <= TOO_MANY_CONNECTIONS_FAST_RETRIES:
                return self.backoff.jittered(TOO_MANY_CONNECTIONS_RETRY_SECONDS), "backoff"
        else:
            log.warning("stream: TooManyConnections (another client holds the connection)")
        return self.backoff.next_delay("rate_limit", reset_at=reset_at), "backoff"

    async def _on_http_error(self, err: XApiError, headers: Any) -> _Retry:
        msg = str(err)
        if isinstance(err, TooManyConnections):
            self._mark_down(msg)
            return await self._too_many_connections(err.rate_limit or RateLimitInfo.from_headers(headers))
        if isinstance(err, CreditsDepleted):
            return self._credits_depleted(msg)
        if isinstance(err, RateLimited) or err.status == 429:
            self._mark_down(msg)
            rl = err.rate_limit or RateLimitInfo.from_headers(headers)
            delay = self.backoff.next_delay("rate_limit", reset_at=rl.reset_at)
            log.warning("stream: rate limited (%s); retry in %.0fs", msg, delay)
            return delay, "backoff"
        if err.status == 403:
            self._forbidden_streak += 1
            if indicates_no_stream_access(err) or self._forbidden_streak >= MAX_CONSECUTIVE_FORBIDDEN:
                self._mark_down(f"filtered stream not available: {msg}")
                self.status.state = "down"
                log.error("stream: filtered stream is not available for this app/plan (%s)", msg)
                raise StreamUnavailable(f"filtered stream not available for this app/plan: {msg}")
        self._mark_down(msg)
        delay = self.backoff.next_delay("http")
        if isinstance(err, NotAuthorized):
            log.error("stream: X rejected the credentials (%s); check X_BEARER_TOKEN. Retry in %.0fs", msg, delay)
        else:
            log.warning("stream: HTTP error (%s); retry in %.0fs", msg, delay)
        return delay, "backoff"
