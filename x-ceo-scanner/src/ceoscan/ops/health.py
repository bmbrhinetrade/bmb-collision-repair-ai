"""HTTP health endpoint: stream status, last post received, today's posts/alerts/latency.

``GET /health`` (and ``GET /``) return a JSON snapshot. ``GET /health?strict=1`` returns HTTP 503
when the overall status is "down", so an uptime monitor can page on it. A configuration problem
(state ``config_error``: the service is idle and sends no post alerts) answers 503 even without
``strict``, so the Docker healthcheck marks the container unhealthy.

Overall status:

* ``down``     - configuration problem, credits depleted, ingest stopped, or not connected for longer
                 than ``ops.stream_down_alert_seconds`` (the same threshold as the stream-down alert);
* ``degraded`` - reconnecting / backing off / starting, heartbeat stale, or the queue almost full;
* ``ok``       - connected (stream) or polling, with fresh heartbeats.

The ``claude_desk`` block reports the Claude analyst desk (enabled, configured, fires today, last fire,
last error, queued, budget left this hour), the ``claude_duet`` block the Claude Duet feed (emails today,
last email, last error, queued, cap left this hour - never an address); neither affects the overall status.
``today`` also counts red flags (``red_flags`` with ``red_flags_unverified``, ``spreading``, ``denials``,
``compromises`` and ``red_flag_posts``: bad-news posts about my tickers seen, alerted or not).
"""

from __future__ import annotations

import asyncio
import json
import logging
import sqlite3
from datetime import datetime
from functools import partial
from typing import TYPE_CHECKING, Any

from aiohttp import web

from .. import __version__
from ..alerts.claude_desk import desk_health
from ..alerts.duet_feed import duet_health
from ..config import Settings
from ..models import IngestStatus
from ..store import Store
from ..timeutil import format_local, from_ms, local_day_start, now_utc

if TYPE_CHECKING:  # pragma: no cover - typing only
    from ..alerts.claude_desk import ClaudeDesk
    from ..alerts.duet_feed import DuetFeed

logger = logging.getLogger(__name__)

HEALTHY_STATES = frozenset({"connected", "polling"})
CONFIG_ERROR_STATE = "config_error"
QUEUE_DEGRADED_FRACTION = 0.9
_TRUE_VALUES = frozenset({"1", "true", "yes", "on"})


def _iso(dt: datetime | None) -> str | None:
    return dt.isoformat() if dt is not None else None


def _age(now: datetime, dt: datetime | None) -> float | None:
    return round(max((now - dt).total_seconds(), 0.0), 1) if dt is not None else None


AUTH_REJECTED_REASON = (
    "X rejects the bearer token (HTTP 401): put the current token in .env"
    " (servers set up with install-server.sh: run it with --update-x-token)"
)


def assess_health(
    status: IngestStatus,
    settings: Settings,
    now: datetime,
    started_at: datetime,
    queue_size: int | None = None,
) -> tuple[str, list[str]]:
    """Return ("ok"|"degraded"|"down", reasons). Pure; used by the snapshot and by tests."""
    down_after = float(settings.ops.stream_down_alert_seconds)
    reasons: list[str] = []
    state = status.state
    level = "ok"

    if state == CONFIG_ERROR_STATE:
        detail = status.last_error or "see the log"
        prefix = "configuration problem: "
        if detail.lower().startswith(prefix):
            detail = detail[len(prefix) :]
        return "down", [f"{prefix}{detail} - fix the configuration and restart ceoscan"]
    if state == "credits_depleted":
        return "down", ["X API credits depleted - add credits at console.x.com"]
    if state == "stopped":
        return "down", ["ingest stopped"]

    if status.down_since is not None:
        down_for = max((now - status.down_since).total_seconds(), 0.0)
        auth = [AUTH_REJECTED_REASON] if status.x_auth_rejected() else []
        if down_for > down_after:
            return "down", [f"not connected for {down_for:.0f}s (> {down_after:.0f}s) - state {state}", *auth]
        level = "degraded"
        reasons.append(f"not connected for {down_for:.0f}s - state {state}")
        reasons.extend(auth)
    elif state == "down":
        return "down", [status.last_error or "ingest is down"]
    elif state == "starting":
        uptime = (now - started_at).total_seconds()
        if uptime > down_after:
            return "down", [f"still starting {uptime:.0f}s after launch"]
        level = "degraded"
        reasons.append("starting")
    elif state not in HEALTHY_STATES:
        level = "degraded"
        reasons.append(f"state {state}")
    else:
        hb_timeout = float(settings.x_api.stream.heartbeat_timeout_seconds)
        stale_after = 2 * hb_timeout
        if state == "polling" and status.poll_interval_seconds:
            stale_after = max(stale_after, 2 * status.poll_interval_seconds)
        hb_age = _age(now, status.last_heartbeat_at)
        if hb_age is not None and hb_age > stale_after:
            level = "degraded"
            reasons.append(f"no data from X for {hb_age:.0f}s")

    queue_max = settings.ops.queue_max
    if queue_size is not None and queue_max > 0 and queue_size >= QUEUE_DEGRADED_FRACTION * queue_max:
        level = "degraded"
        reasons.append(f"pipeline queue almost full ({queue_size}/{queue_max})")
    return level, reasons


class HealthServer:
    """aiohttp.web server exposing the health snapshot."""

    def __init__(
        self,
        host: str,
        port: int,
        status: IngestStatus,
        store: Store,
        settings: Settings,
        started_at: datetime,
        queue: asyncio.Queue | None = None,
        desk: ClaudeDesk | None = None,
        desk_problem: str | None = None,
        duet: DuetFeed | None = None,
        duet_problem: str | None = None,
    ) -> None:
        self.host = host
        self.port = port
        self.status = status
        self.store = store
        self.settings = settings
        self.started_at = started_at
        self.queue = queue
        #: the Claude analyst desk (set by the service once it exists) and why it is off, if it is
        self.desk = desk
        self.desk_problem = desk_problem
        #: the Claude Duet feed (set by the service once it exists) and why it is off, if it is
        self.duet = duet
        self.duet_problem = duet_problem
        self._runner: web.AppRunner | None = None

    # ------------------------------------------------------------------ snapshot

    def _last_post(self, now: datetime) -> dict[str, Any]:
        tz = self.settings.alerts.timezone
        row: dict[str, Any] | None = None
        try:
            row = self.store.last_post()
        except sqlite3.Error as exc:
            logger.warning("health: last_post query failed: %s", exc)
        post_id = self.status.last_post_id
        at = self.status.last_post_at
        handle: str | None = None
        created: datetime | None = None
        if row:
            row_at = from_ms(int(row["received_at_ms"]))
            if at is None or row_at >= at or row["id"] == post_id:
                post_id, at = str(row["id"]), row_at
                handle = row.get("author_username")
                created = from_ms(int(row["created_at_ms"]))
        return {
            "id": post_id,
            "at": _iso(at),
            "at_local": format_local(at, tz) if at else None,
            "created_at": _iso(created),
            "handle": handle,
            "seconds_ago": _age(now, at),
        }

    def _today(self, now: datetime) -> dict[str, Any]:
        since = local_day_start(now, self.settings.alerts.timezone)
        try:
            stats = self.store.stats_since(since)
        except sqlite3.Error as exc:
            logger.warning("health: stats query failed: %s", exc)
            return {"since": _iso(since), "error": "stats unavailable"}
        return {
            "since": _iso(since),
            "posts": stats.get("posts", 0),
            "flagged": stats.get("flagged", 0),
            "claude_scored": stats.get("claude_scored", 0),
            "alerts": stats.get("alerts", 0),
            "alerts_late": stats.get("alerts_late", 0),
            "alerts_failed": stats.get("alerts_failed", 0),
            # live alerts only: late catch-up alerts (missed during an outage) are left out
            "avg_latency_ms": stats.get("avg_latency_ms"),
            "max_latency_ms": stats.get("max_latency_ms"),
            "score_errors": stats.get("score_errors", 0),
            "stream_errors": stats.get("stream_errors", 0),
            "errors": stats.get("errors", 0),
            "desk_fires": stats.get("desk_fires", 0),
            "desk_errors": stats.get("desk_errors", 0),
            "duet_emails": stats.get("duet_emails", 0),
            "duet_errors": stats.get("duet_errors", 0),
            # red flags: bad-news alerts about my tickers (incl. "spreading"), how many of those were unverified
            # or likely false, spreading / denied / possible-hijack alerts, and bad-news posts seen (alerted or not)
            "red_flags": stats.get("red_flags", 0),
            "red_flags_unverified": stats.get("red_flags_unverified", 0),
            "spreading": stats.get("spreading", 0),
            "denials": stats.get("denials", 0),
            "compromises": stats.get("compromises", 0),
            "red_flag_posts": stats.get("red_flag_posts", 0),
        }

    def snapshot(self, now: datetime | None = None) -> dict:
        """JSON-able health snapshot (no secrets: last_error is built from X problem titles only)."""
        now = now or now_utc()
        st = self.status
        queue_size = self.queue.qsize() if self.queue is not None else None
        overall, reasons = assess_health(st, self.settings, now, self.started_at, queue_size)
        return {
            "status": overall,
            "state": st.state,  # e.g. "config_error" when the service is idle because of a configuration problem
            "reasons": reasons,
            "mode": st.mode,
            "stream": {
                "state": st.state,
                "connected_since": _iso(st.connected_since),
                "last_heartbeat_at": _iso(st.last_heartbeat_at),
                "heartbeat_seconds_ago": _age(now, st.last_heartbeat_at),
                "down_since": _iso(st.down_since),
                "down_seconds": _age(now, st.down_since),
                "reconnects": st.reconnects,
                "last_error": st.last_error,
                "poll_interval_seconds": st.poll_interval_seconds,
                "estimated_latency_seconds": st.estimated_latency_seconds,
            },
            "last_post": self._last_post(now),
            "today": self._today(now),
            "claude_desk": desk_health(
                self.settings.claude_desk, self.store, self.desk, now, self.settings.alerts.timezone,
                self.desk_problem,
            ),
            "claude_duet": duet_health(
                self.settings.claude_duet, self.store, self.duet, now, self.settings.alerts.timezone,
                self.duet_problem,
            ),
            "queue_size": queue_size,
            "uptime_seconds": round(max((now - self.started_at).total_seconds(), 0.0), 1),
            "version": __version__,
            "time": _iso(now),
        }

    # ------------------------------------------------------------------ http

    async def _handle(self, request: web.Request) -> web.Response:
        strict = request.query.get("strict", "").strip().lower() in _TRUE_VALUES
        try:
            snap = self.snapshot()
        except Exception:  # never 500 without a body; keep the monitor informative
            logger.exception("health: snapshot failed")
            return web.json_response(
                {"status": "down", "reasons": ["health snapshot failed"], "version": __version__},
                status=503 if strict else 500,
            )
        down = snap["status"] == "down"
        http_status = 503 if down and (strict or snap.get("state") == CONFIG_ERROR_STATE) else 200
        return web.json_response(
            snap,
            status=http_status,
            dumps=partial(json.dumps, default=str),
            headers={"Cache-Control": "no-store"},
        )

    def make_app(self) -> web.Application:
        app = web.Application()
        app.router.add_get("/health", self._handle)
        app.router.add_get("/", self._handle)
        return app

    async def start(self) -> None:
        if self._runner is not None:
            return
        runner = web.AppRunner(self.make_app(), access_log=None)  # monitors poll often; keep logs clean
        await runner.setup()
        site = web.TCPSite(runner, self.host, self.port)
        try:
            await site.start()
        except BaseException:
            await runner.cleanup()
            raise
        self._runner = runner
        # Report the real port (useful when port=0 picks a free one).
        for addr in runner.addresses:
            if isinstance(addr, tuple) and len(addr) >= 2:
                self.port = int(addr[1])
                break
        logger.info("health: listening on http://%s:%d/health", self.host, self.port)

    async def stop(self) -> None:
        runner, self._runner = self._runner, None
        if runner is not None:
            await runner.cleanup()
            logger.info("health: stopped")
