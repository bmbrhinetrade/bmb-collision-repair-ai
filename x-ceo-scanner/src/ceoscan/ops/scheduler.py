"""Ops scheduler: stream-down / recovered alerts and the daily summary.

* Stream down: when ``status.down_since`` is older than ``ops.stream_down_alert_seconds`` (default
  120 s) one system alert is sent ("X stream down", or "X API credits depleted ..." when that is the
  cause). When ingest is healthy again, one "recovered after Xm Ys" alert follows.
* Daily summary: at ``ops.daily_summary.time`` local time (``alerts.timezone``, default 18:00 PT),
  once per local date (kv ``last_daily_summary``), and only within 60 minutes after the scheduled
  time, so a restart late in the evening does not send a stale summary.

All ops alerts are recorded with ``store.record_alert`` and ``store.log_event``.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Literal
from zoneinfo import ZoneInfo

from ..config import AppConfig
from ..models import AlertMessage, AlertRecord, ChannelResult, IngestStatus, Priority
from ..store import Store
from ..timeutil import format_local, local_day_start, now_utc, parse_hhmm

if TYPE_CHECKING:  # pragma: no cover - typing only
    from ..alerts.dispatcher import AlertDispatcher

logger = logging.getLogger(__name__)

KV_LAST_DAILY_SUMMARY = "last_daily_summary"
SUMMARY_WINDOW = timedelta(minutes=60)
HEALTHY_STATES = frozenset({"connected", "polling"})
DOWN_STATES = frozenset({"down", "credits_depleted"})
SMS_MAX_CHARS = 300


def format_duration(seconds: float) -> str:
    """3725 -> "1h 2m 5s", 192 -> "3m 12s", 45 -> "45s"."""
    total = max(round(seconds), 0)
    hours, rem = divmod(total, 3600)
    minutes, secs = divmod(rem, 60)
    if hours:
        return f"{hours}h {minutes}m {secs}s"
    if minutes:
        return f"{minutes}m {secs}s"
    return f"{secs}s"


def _system_message(
    title: str, body: str, priority: Priority, kind: Literal["system", "summary"] = "system"
) -> AlertMessage:
    one_line = " ".join(f"ceoscan: {title} - {body}".split())
    if len(one_line) > SMS_MAX_CHARS:
        one_line = one_line[: SMS_MAX_CHARS - 3].rstrip() + "..."
    return AlertMessage(
        title=title,
        body=body,
        url=None,
        priority=priority,
        tags=["warning"] if priority in ("high", "urgent") else [],
        sms_text=one_line,
        kind=kind,
    )


class OpsScheduler:
    """Periodic ops checks. One instance per service; state is in memory (plus kv for the summary)."""

    def __init__(
        self,
        cfg: AppConfig,
        store: Store,
        status: IngestStatus,
        dispatcher: AlertDispatcher,
        clock: Callable[[], datetime] = now_utc,
    ) -> None:
        self.cfg = cfg
        self.settings = cfg.settings
        self.store = store
        self.status = status
        self.dispatcher = dispatcher
        self.clock = clock
        self._down_alert_since: datetime | None = None  # outage we already alerted about
        self._observed_bad_since: datetime | None = None  # fallback when down_since is not set

    # ------------------------------------------------------------------ loop

    async def run(self, stop: asyncio.Event, tick_seconds: float = 5.0) -> None:
        """Tick until ``stop`` is set. Errors in one check are logged and never stop the loop."""
        logger.info(
            "ops: scheduler started (stream-down alert after %.0fs, daily summary %s %s)",
            self.settings.ops.stream_down_alert_seconds,
            self.settings.ops.daily_summary.time if self.settings.ops.daily_summary.enabled else "off",
            self.settings.alerts.timezone,
        )
        while not stop.is_set():
            now = self.clock()
            for check in (self.check_stream_down, self.maybe_send_daily_summary):
                try:
                    await check(now)
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    logger.exception("ops: %s failed", check.__name__)
                    self._log_event("ops_error", f"{check.__name__}: {type(exc).__name__}: {exc}", "error")
            try:
                await asyncio.wait_for(stop.wait(), timeout=tick_seconds)
            except TimeoutError:
                pass
        logger.info("ops: scheduler stopped")

    # ------------------------------------------------------------------ helpers

    def _log_event(self, kind: str, detail: str, level: str) -> None:
        try:
            self.store.log_event(kind=kind, detail=detail, level=level)
        except Exception:  # pragma: no cover - never let bookkeeping break alerting
            logger.exception("ops: log_event failed")

    async def _send(self, msg: AlertMessage) -> list[ChannelResult]:
        try:
            return await self.dispatcher.send(msg)
        except Exception as exc:  # dispatcher contract: never raises; be defensive anyway
            logger.exception("ops: dispatcher failed for %r", msg.title)
            return [ChannelResult(channel="dispatcher", ok=False, elapsed_ms=0, error=type(exc).__name__)]

    async def _send_and_record(self, msg: AlertMessage, reason: str) -> AlertRecord:
        results = await self._send(msg)
        rec = AlertRecord(post_id=None, kind=msg.kind, decision_reason=reason, message=msg, results=results)
        try:
            self.store.record_alert(rec)
        except Exception:  # pragma: no cover - never let bookkeeping break alerting
            logger.exception("ops: record_alert failed")
        if not rec.sent_ok:
            failed = ", ".join(f"{r.channel}: {r.error or 'failed'}" for r in results) or "no channels"
            logger.error("ops: %r could not be delivered (%s)", msg.title, failed)
        return rec

    def _effective_down_since(self, now: datetime) -> datetime | None:
        """status.down_since, or when we first saw a down state without it."""
        st = self.status
        if st.down_since is not None:
            self._observed_bad_since = None
            return st.down_since
        if st.state in DOWN_STATES:
            if self._observed_bad_since is None:
                self._observed_bad_since = now
            return self._observed_bad_since
        self._observed_bad_since = None
        return None

    def _ingest_label(self) -> str:
        return "timeline polling" if self.status.mode == "poll" else "stream"

    # ------------------------------------------------------------------ stream down / recovered

    async def check_stream_down(self, now: datetime) -> AlertRecord | None:
        """Send one alert when ingest has been down > threshold, and one when it recovers."""
        st = self.status
        threshold = float(self.settings.ops.stream_down_alert_seconds)
        tz = self.settings.alerts.timezone
        down_since = self._effective_down_since(now)

        if down_since is not None:
            if self._down_alert_since is not None:
                return None  # already alerted for this outage
            down_for = (now - down_since).total_seconds()
            if down_for <= threshold:
                return None
            self._down_alert_since = down_since
            if st.state == "credits_depleted":
                title = "X API credits depleted - add credits at console.x.com"
                cause = "X returned 402 (credits depleted)"
            else:
                title = f"X {self._ingest_label()} down"
                cause = f"state {st.state}"
            lines = [
                f"No connection to X for {format_duration(down_for)} (since {format_local(down_since, tz)}); {cause}.",
                "No post alerts can arrive until it reconnects; it keeps retrying automatically.",
            ]
            if st.last_error:
                lines.append(f"Last error: {st.last_error}")
            msg = _system_message(title, "\n".join(lines), "high")
            reason = f"{self._ingest_label()} down {down_for:.0f}s > {threshold:.0f}s"
            self._log_event("stream_down_alert", f"{title}: down {down_for:.0f}s, state {st.state}", "error")
            logger.error("ops: %s (down %s, state %s)", title, format_duration(down_for), st.state)
            return await self._send_and_record(msg, reason)

        if self._down_alert_since is None or st.state not in HEALTHY_STATES:
            return None
        outage_start = self._down_alert_since
        back_at = st.connected_since or now
        if back_at < outage_start:
            back_at = now
        downtime = (back_at - outage_start).total_seconds()
        self._down_alert_since = None
        title = f"X {self._ingest_label()} recovered after {format_duration(downtime)}"
        lines = [f"Back to {st.state} since {format_local(back_at, tz)}."]
        if st.mode == "stream" and self.settings.x_api.recovery.enabled:
            lines.append("Posts missed during the outage are being fetched from the accounts' timelines.")
        msg = _system_message(title, "\n".join(lines), "normal")
        self._log_event("stream_recovered_alert", f"{title}", "info")
        logger.info("ops: %s", title)
        return await self._send_and_record(msg, f"{self._ingest_label()} recovered after {downtime:.0f}s")

    # ------------------------------------------------------------------ daily summary

    def _scheduled_at(self, now: datetime) -> datetime:
        zone = ZoneInfo(self.settings.alerts.timezone)
        local = now.astimezone(zone)
        t = parse_hhmm(self.settings.ops.daily_summary.time)
        return datetime(local.year, local.month, local.day, t.hour, t.minute, tzinfo=zone)

    async def maybe_send_daily_summary(self, now: datetime) -> AlertRecord | None:
        """Send the daily summary once per local date, within 60 min after the scheduled time."""
        ds = self.settings.ops.daily_summary
        if not ds.enabled:
            return None
        scheduled = self._scheduled_at(now)
        local = now.astimezone(scheduled.tzinfo)
        if not scheduled <= local < scheduled + SUMMARY_WINDOW:
            return None
        day = local.date().isoformat()
        if self.store.kv_get(KV_LAST_DAILY_SUMMARY) == day:
            return None
        # Mark first: a failing channel must not cause a resend on every tick.
        self.store.kv_set(KV_LAST_DAILY_SUMMARY, day)
        msg = self.build_summary(now)
        rec = await self._send_and_record(msg, f"daily summary {ds.time} {self.settings.alerts.timezone}")
        self._log_event("daily_summary", f"{day}: sent_ok={rec.sent_ok}", "info")
        logger.info("ops: daily summary for %s sent (ok=%s)", day, rec.sent_ok)
        return rec

    def build_summary(self, now: datetime) -> AlertMessage:
        """Today's (local day) numbers as a low-priority summary message."""
        tz = self.settings.alerts.timezone
        since = local_day_start(now, tz)
        stats = self.store.stats_since(since)
        posts = int(stats.get("posts") or 0)
        alerts = int(stats.get("alerts") or 0)
        failed = int(stats.get("alerts_failed") or 0)
        errors = int(stats.get("errors") or 0)
        avg_ms, max_ms = stats.get("avg_latency_ms"), stats.get("max_latency_ms")

        error_kinds: list[str] = []
        if errors:
            for ev in self.store.recent_events(since, level="error", limit=50):
                kind = str(ev.get("kind"))
                if kind not in error_kinds:
                    error_kinds.append(kind)
                if len(error_kinds) == 3:
                    break

        pricing = self.settings.x_api.pricing
        projected = posts * pricing.post_read * 30
        st = self.status
        local = now.astimezone(ZoneInfo(tz))

        if avg_ms is not None:
            latency = f"avg {avg_ms / 1000:.1f}s, max {(max_ms or 0) / 1000:.1f}s"
        else:
            latency = "n/a (no alerts sent)"
        errors_line = f"{errors}" + (f" (latest: {', '.join(error_kinds)})" if error_kinds else "")
        body_lines = [
            f"Posts seen: {posts} (flagged {stats.get('flagged') or 0}, Claude-scored {stats.get('claude_scored') or 0})",
            f"Alerts sent: {alerts}" + (f" ({failed} failed)" if failed else " (0 failed)"),
            f"Latency: {latency}",
            f"Errors: {errors_line}" if errors else "Errors: none",
            f"Ingest: {st.mode} / {st.state}, {st.reconnects} reconnect(s) since start",
            (
                f"Projected X cost at today's volume: ${projected:,.2f}/month "
                f"({posts} posts x ${pricing.post_read:g} x 30)"
            ),
        ]
        title = f"ceoscan daily summary - {local:%b} {local.day}"
        sms = (
            f"ceoscan {local:%b} {local.day}: {posts} posts, {alerts} alerts"
            + (f" ({failed} failed)" if failed else "")
            + (f", avg {avg_ms / 1000:.1f}s" if avg_ms is not None else "")
            + f", {errors} errors"
        )
        return AlertMessage(
            title=title,
            body="\n".join(body_lines),
            url=None,
            priority="low",
            tags=["summary"],
            sms_text=sms[:SMS_MAX_CHARS],
            kind="summary",
        )
