"""Ops scheduler: stream-down / recovered alerts, the daily summary and the config-problem alert.

* Stream down: when ``status.down_since`` is older than ``ops.stream_down_alert_seconds`` (default
  120 s) a system alert is sent ("X stream down", or "X API credits depleted ..." when that is the
  cause). The alert is latched only once a channel delivered it: a failed send is retried after
  60 s, 120 s, 300 s, ... (at most every 15 min) for as long as the outage lasts. When ingest is
  healthy again, one "recovered after Xm Ys" alert follows (retried a few times if it fails), and
  it says so if the "down" alert never got through.
* Daily summary: at ``ops.daily_summary.time`` local time (``alerts.timezone``, default 18:00 PT),
  once per local date, and only within 60 minutes after the scheduled time, so a restart late in
  the evening does not send a stale summary. kv ``last_daily_summary`` is set only after a channel
  delivered it; a failed send is retried every 5 minutes while the window is open (the attempt
  time is kept in kv ``daily_summary_attempt``, so restarts do not resend on every start).
* :func:`send_config_error_alert`: at most one "ceoscan stopped: configuration problem" alert per
  distinct problem for ``run`` when even the configuration cannot be loaded (kv
  ``config_problem_alert``, shared with ``app.Service``), so a restart loop can never spam the phone.

All ops alerts are recorded with ``store.record_alert`` and ``store.log_event``.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
from collections.abc import Callable
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Any, Literal
from zoneinfo import ZoneInfo

from ..config import AppConfig
from ..models import AlertMessage, AlertRecord, ChannelResult, IngestStatus, Priority
from ..store import Store
from ..timeutil import format_local, local_day_start, now_utc, parse_hhmm

if TYPE_CHECKING:  # pragma: no cover - typing only
    from ..alerts.dispatcher import AlertDispatcher

logger = logging.getLogger(__name__)

KV_LAST_DAILY_SUMMARY = "last_daily_summary"
KV_DAILY_SUMMARY_ATTEMPT = "daily_summary_attempt"
KV_CONFIG_ERROR_ALERT = "config_problem_alert"  # shared with app.Service (same {"hash", "at"} format)
CONFIG_ERROR_ALERT_TITLE = "ceoscan stopped: configuration problem"
CONFIG_ERROR_ALERT_REPEAT = timedelta(hours=24)
SUMMARY_WINDOW = timedelta(minutes=60)
SUMMARY_RETRY = timedelta(minutes=5)
#: Delays between retries of an undelivered "stream down" alert (the last one repeats).
DOWN_ALERT_RETRY_SECONDS: tuple[float, ...] = (60.0, 120.0, 300.0, 600.0, 900.0)
RECOVERY_ALERT_RETRY = timedelta(seconds=60)
RECOVERY_ALERT_MAX_ATTEMPTS = 3
#: The projected monthly X cost needs at least this much data from the running service.
MIN_PROJECTION_WINDOW = timedelta(hours=1)
PROJECTION_WINDOW = timedelta(hours=24)
HEALTHY_STATES = frozenset({"connected", "polling"})
DOWN_STATES = frozenset({"down", "credits_depleted"})
CONFIG_ERROR_STATE = "config_error"
SMS_MAX_CHARS = 300
SCORE_ERROR_CHARS = 120


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


def _delivered_or_unsendable(results: list[ChannelResult]) -> bool:
    """True when a channel delivered the message, or when there is no channel at all (nothing to retry)."""
    return not results or any(r.ok for r in results)


async def _dispatch(dispatcher: Any, msg: AlertMessage) -> list[ChannelResult]:
    try:
        return list(await dispatcher.send(msg))
    except Exception as exc:  # dispatcher contract: never raises; be defensive anyway
        logger.exception("ops: dispatcher failed for %r", msg.title)
        return [ChannelResult(channel="dispatcher", ok=False, elapsed_ms=0, error=type(exc).__name__)]


# --------------------------------------------------------------------------- configuration problem alert


def config_error_fingerprint(message: str) -> str:
    """Short hash of a configuration-problem message (the same digest ``app.Service`` stores)."""
    return hashlib.sha256(message.encode("utf-8", errors="replace")).hexdigest()[:16]


def _parse_iso(value: Any) -> datetime | None:
    try:
        dt = datetime.fromisoformat(str(value))
    except (TypeError, ValueError):
        return None
    return dt if dt.tzinfo is not None else None


async def send_config_error_alert(
    store: Store | None,
    dispatcher: Any,
    message: str,
    action: str = "exit",
    now: datetime | None = None,
) -> AlertRecord | None:
    """Send at most one "ceoscan stopped: configuration problem: ..." alert per distinct problem.

    For problems found before the service exists (``run`` could not even load its configuration);
    ``app.Service`` handles the ones it finds itself the same way. kv ``config_problem_alert``
    holds ``{"hash", "at"}`` of the last alerted message, shared with ``app.Service``: the same
    problem is not alerted again for 24 h, so a restart loop can never spam the phone. The marker
    is written before sending (a crash during the send must not cause a repeat); the service
    clears it once ingest has been up for a while. ``store`` may be None (the database itself is
    the problem): then the alert is sent without dedupe. ``action`` is ``ops.config_error_action``
    and only changes the wording. Never raises.
    """
    now = now or now_utc()
    digest = config_error_fingerprint(message)
    if store is not None:
        try:
            raw = store.kv_get(KV_CONFIG_ERROR_ALERT)
            prev = json.loads(raw) if raw else None
        except Exception:  # a broken store must not block the alert
            prev = None
        if isinstance(prev, dict) and prev.get("hash") == digest:
            at = _parse_iso(prev.get("at"))
            if at is not None and timedelta(0) <= now - at < CONFIG_ERROR_ALERT_REPEAT:
                logger.info("ops: configuration-problem alert already sent at %s; not repeating it", at.isoformat())
                return None
        try:
            store.kv_set(KV_CONFIG_ERROR_ALERT, json.dumps({"hash": digest, "at": now.isoformat()}))
        except Exception:
            logger.warning("ops: could not record the configuration-problem alert; it may be repeated")
    short = " ".join(message.split())
    if len(short) > 120:
        short = short[:117].rstrip() + "..."
    if action == "wait":
        what = "It stays up idle (/health: state config_error) - fix the configuration and restart it."
    else:
        what = "It exited (code 2) and will not restart by itself - fix the configuration and start it again."
    msg = _system_message(
        f"{CONFIG_ERROR_ALERT_TITLE}: {short}", f"{message}\nceoscan is not watching X. {what}", "high"
    )
    results = await _dispatch(dispatcher, msg)
    rec = AlertRecord(post_id=None, kind="system", decision_reason="config_problem", message=msg, results=results)
    if store is not None:
        try:
            store.record_alert(rec)
            store.log_event("config_problem", msg.title[:300], "error")
        except Exception:  # bookkeeping must never break alerting
            logger.exception("ops: could not record the configuration-problem alert")
    if results and not rec.sent_ok:
        failed = ", ".join(f"{r.channel}: {r.error or 'failed'}" for r in results)
        logger.error("ops: configuration-problem alert could not be delivered (%s)", failed)
    return rec


# --------------------------------------------------------------------------- scheduler


class OpsScheduler:
    """Periodic ops checks. One instance per service; state is in memory (plus kv for the summary)."""

    def __init__(
        self,
        cfg: AppConfig,
        store: Store,
        status: IngestStatus,
        dispatcher: AlertDispatcher,
        clock: Callable[[], datetime] = now_utc,
        started_at: datetime | None = None,
    ) -> None:
        self.cfg = cfg
        self.settings = cfg.settings
        self.store = store
        self.status = status
        self.dispatcher = dispatcher
        self.clock = clock
        #: when this service started collecting posts (the cost projection only uses data since then)
        self.started_at = started_at or clock()
        self._down_alert_since: datetime | None = None  # outage we alerted (or tried to alert) about
        self._down_alert_delivered = False
        self._down_alert_attempts = 0
        self._down_next_retry_at: datetime | None = None
        self._observed_bad_since: datetime | None = None  # fallback when down_since is not set
        self._recovery_msg: tuple[AlertMessage, str] | None = None  # undelivered "recovered" alert
        self._recovery_attempts = 0
        self._recovery_next_retry_at: datetime | None = None

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
        return await _dispatch(self.dispatcher, msg)

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

    @staticmethod
    def _down_retry_delay(attempts: int) -> timedelta:
        idx = min(max(attempts - 1, 0), len(DOWN_ALERT_RETRY_SECONDS) - 1)
        return timedelta(seconds=DOWN_ALERT_RETRY_SECONDS[idx])

    # ------------------------------------------------------------------ stream down / recovered

    async def check_stream_down(self, now: datetime) -> AlertRecord | None:
        """Alert when ingest has been down > threshold (retried until delivered), and once when it recovers."""
        st = self.status
        if st.state == CONFIG_ERROR_STATE:
            return None  # the service sends its own one-time "configuration problem" alert
        threshold = float(self.settings.ops.stream_down_alert_seconds)
        down_since = self._effective_down_since(now)

        if down_since is not None:
            self._recovery_msg = None  # a new outage supersedes an undelivered "recovered" alert
            if self._down_alert_since is not None:
                if self._down_alert_delivered:
                    return None  # already alerted for this outage
                if self._down_next_retry_at is not None and now < self._down_next_retry_at:
                    return None
                return await self._send_down_alert(now, self._down_alert_since, threshold, retry=True)
            down_for = (now - down_since).total_seconds()
            if down_for <= threshold:
                return None
            self._down_alert_since = down_since
            self._down_alert_delivered = False
            self._down_alert_attempts = 0
            return await self._send_down_alert(now, down_since, threshold, retry=False)

        if st.state not in HEALTHY_STATES:
            return None
        if self._down_alert_since is None:
            return await self._retry_recovery_alert(now)
        return await self._send_recovery_alert(now)

    async def _send_down_alert(self, now: datetime, down_since: datetime, threshold: float, retry: bool) -> AlertRecord:
        st = self.status
        tz = self.settings.alerts.timezone
        down_for = (now - down_since).total_seconds()
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
        self._down_alert_attempts += 1
        if retry:
            reason += f" (retry {self._down_alert_attempts - 1})"
        else:
            self._log_event("stream_down_alert", f"{title}: down {down_for:.0f}s, state {st.state}", "error")
            logger.error("ops: %s (down %s, state %s)", title, format_duration(down_for), st.state)
        rec = await self._send_and_record(msg, reason)
        if _delivered_or_unsendable(rec.results):
            self._down_alert_delivered = True
            self._down_next_retry_at = None
        else:
            delay = self._down_retry_delay(self._down_alert_attempts)
            self._down_next_retry_at = now + delay
            self._log_event(
                "stream_down_alert_failed",
                f"attempt {self._down_alert_attempts} not delivered; retry in {format_duration(delay.total_seconds())}",
                "error",
            )
            logger.error(
                "ops: %r not delivered (attempt %d); retrying in %s while the outage lasts",
                title,
                self._down_alert_attempts,
                format_duration(delay.total_seconds()),
            )
        return rec

    async def _send_recovery_alert(self, now: datetime) -> AlertRecord:
        st = self.status
        tz = self.settings.alerts.timezone
        outage_start = self._down_alert_since
        assert outage_start is not None
        back_at = st.connected_since or now
        if back_at < outage_start:
            back_at = now
        downtime = (back_at - outage_start).total_seconds()
        down_delivered, down_attempts = self._down_alert_delivered, self._down_alert_attempts
        self._down_alert_since = None
        self._down_alert_delivered = False
        self._down_alert_attempts = 0
        self._down_next_retry_at = None
        title = f"X {self._ingest_label()} recovered after {format_duration(downtime)}"
        lines = [f"Back to {st.state} since {format_local(back_at, tz)}."]
        if not down_delivered:
            lines.append(
                f"The 'down' alert for this outage could not be delivered ({down_attempts} attempt(s)); "
                f"X was unreachable from {format_local(outage_start, tz)}."
            )
        if st.mode == "stream" and self.settings.x_api.recovery.enabled:
            lines.append("Posts the watched accounts made during the outage are being fetched from their timelines.")
        msg = _system_message(title, "\n".join(lines), "normal")
        reason = f"{self._ingest_label()} recovered after {downtime:.0f}s"
        self._log_event("stream_recovered_alert", f"{title}", "info")
        logger.info("ops: %s", title)
        rec = await self._send_and_record(msg, reason)
        self._recovery_attempts = 1
        if _delivered_or_unsendable(rec.results):
            self._recovery_msg = None
        else:
            self._recovery_msg = (msg, reason)
            self._recovery_next_retry_at = now + RECOVERY_ALERT_RETRY
        return rec

    async def _retry_recovery_alert(self, now: datetime) -> AlertRecord | None:
        if self._recovery_msg is None:
            return None
        if self._recovery_next_retry_at is not None and now < self._recovery_next_retry_at:
            return None
        msg, reason = self._recovery_msg
        self._recovery_attempts += 1
        rec = await self._send_and_record(msg, f"{reason} (retry {self._recovery_attempts - 1})")
        if _delivered_or_unsendable(rec.results) or self._recovery_attempts >= RECOVERY_ALERT_MAX_ATTEMPTS:
            if not rec.sent_ok and rec.results:
                logger.error("ops: giving up on %r after %d attempts", msg.title, self._recovery_attempts)
            self._recovery_msg = None
        else:
            self._recovery_next_retry_at = now + RECOVERY_ALERT_RETRY
        return rec

    # ------------------------------------------------------------------ daily summary

    def _scheduled_at(self, now: datetime) -> datetime:
        zone = ZoneInfo(self.settings.alerts.timezone)
        local = now.astimezone(zone)
        t = parse_hhmm(self.settings.ops.daily_summary.time)
        return datetime(local.year, local.month, local.day, t.hour, t.minute, tzinfo=zone)

    def _summary_attempt(self) -> dict[str, Any]:
        raw = self.store.kv_get(KV_DAILY_SUMMARY_ATTEMPT)
        if not raw:
            return {}
        try:
            data = json.loads(raw)
        except ValueError:
            return {}
        return data if isinstance(data, dict) else {}

    async def maybe_send_daily_summary(self, now: datetime) -> AlertRecord | None:
        """Send the daily summary once per local date, within 60 min after the scheduled time.

        Marked as sent only after a channel delivered it; failed sends are retried every 5 minutes
        while the window is open.
        """
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
        attempt = self._summary_attempt()
        attempts = int(attempt.get("attempts") or 0) if attempt.get("day") == day else 0
        if attempts:
            try:
                last_at = datetime.fromisoformat(str(attempt.get("at")))
            except ValueError:
                last_at = None
            if last_at is not None and now < last_at + SUMMARY_RETRY:
                return None  # a failing channel must not cause a resend on every tick
        attempts += 1
        self.store.kv_set(
            KV_DAILY_SUMMARY_ATTEMPT, json.dumps({"day": day, "at": now.isoformat(), "attempts": attempts})
        )
        msg = self.build_summary(now)
        reason = f"daily summary {ds.time} {self.settings.alerts.timezone}"
        if attempts > 1:
            reason += f" (retry {attempts - 1})"
        rec = await self._send_and_record(msg, reason)
        if _delivered_or_unsendable(rec.results):
            self.store.kv_set(KV_LAST_DAILY_SUMMARY, day)
            self._log_event("daily_summary", f"{day}: sent_ok={rec.sent_ok}, attempt {attempts}", "info")
            logger.info("ops: daily summary for %s sent (ok=%s, attempt %d)", day, rec.sent_ok, attempts)
        else:
            self._log_event("daily_summary_failed", f"{day}: attempt {attempts} not delivered", "error")
            logger.error(
                "ops: daily summary for %s not delivered (attempt %d); retrying in %s while the window is open",
                day,
                attempts,
                format_duration(SUMMARY_RETRY.total_seconds()),
            )
        return rec

    def _projection(self, now: datetime) -> tuple[float | None, str]:
        """Projected monthly X cost from the posts received in the last 24 h of this service's run.

        Only data since the service started is used (the database may hold posts from an earlier
        run); with less than 24 h of data the count is scaled up to a full day.
        """
        pricing = self.settings.x_api.pricing
        since = max(now - PROJECTION_WINDOW, self.started_at)
        window = now - since
        if window < MIN_PROJECTION_WINDOW:
            return None, f"n/a (running for {format_duration(window.total_seconds())}; needs 1 h of data)"
        posts = int(self.store.stats_since(since).get("posts") or 0)
        if window >= PROJECTION_WINDOW - timedelta(minutes=1):
            per_day = float(posts)
            basis = f"{posts} posts in the last 24 h x ${pricing.post_read:g} x 30"
        else:
            hours = window.total_seconds() / 3600
            per_day = posts * 24 / hours
            basis = (
                f"{posts} posts in {format_duration(window.total_seconds())} since start, "
                f"scaled to {per_day:,.0f}/day x ${pricing.post_read:g} x 30"
            )
        return per_day * pricing.post_read * 30, basis

    def build_summary(self, now: datetime) -> AlertMessage:
        """Today's (local day) numbers as a low-priority summary message."""
        tz = self.settings.alerts.timezone
        since = local_day_start(now, tz)
        stats = self.store.stats_since(since)
        posts = int(stats.get("posts") or 0)
        alerts = int(stats.get("alerts") or 0)
        late = int(stats.get("alerts_late") or 0)
        failed = int(stats.get("alerts_failed") or 0)
        errors = int(stats.get("errors") or 0)
        score_errors = int(stats.get("score_errors") or 0)
        stream_errors = int(stats.get("stream_errors") or 0)
        avg_ms, max_ms = stats.get("avg_latency_ms"), stats.get("max_latency_ms")

        error_kinds: list[str] = []
        if errors:
            for ev in self.store.recent_events(since, level="error", limit=50):
                kind = str(ev.get("kind"))
                if kind not in error_kinds:
                    error_kinds.append(kind)
                if len(error_kinds) == 3:
                    break

        projected, basis = self._projection(now)
        st = self.status
        local = now.astimezone(ZoneInfo(tz))

        if avg_ms is not None:
            latency = f"avg {avg_ms / 1000:.1f}s, max {(max_ms or 0) / 1000:.1f}s"
            if late:
                latency += " (live alerts; late catch-up alerts excluded)"
        else:
            latency = "n/a (only late catch-up alerts)" if late else "n/a (no alerts sent)"
        errors_line = f"{errors}" + (f" (latest: {', '.join(error_kinds)})" if error_kinds else "")
        alerts_line = f"Alerts sent: {alerts}" + (f" ({failed} failed)" if failed else " (0 failed)")
        if late:
            alerts_line += f", {late} late (missed during an outage)"
        score_line = f"Scoring errors: {score_errors}"
        if score_errors:
            last = " ".join(str(stats.get("last_score_error") or "").split())[:SCORE_ERROR_CHARS]
            score_line += " (rules-only score used" + (f"; latest: {last}" if last else "") + ")"
        body_lines = [
            (
                f"Posts seen: {posts} (flagged {stats.get('flagged') or 0}, "
                f"Claude-scored {stats.get('claude_scored') or 0})"
            ),
            alerts_line,
            f"Latency: {latency}",
            score_line,
            f"Stream errors: {stream_errors}",
            f"Errors: {errors_line}" if errors else "Errors: none",
            f"Ingest: {st.mode} / {st.state}, {st.reconnects} reconnect(s) since start",
            (
                f"Projected X cost: ${projected:,.2f}/month ({basis})"
                if projected is not None
                else f"Projected X cost: {basis}"
            ),
        ]
        title = f"ceoscan daily summary - {local:%b} {local.day}"
        sms = (
            f"ceoscan {local:%b} {local.day}: {posts} posts, {alerts} alerts"
            + (f" ({failed} failed)" if failed else "")
            + (f", avg {avg_ms / 1000:.1f}s" if avg_ms is not None else "")
            + f", {errors} errors"
            + (f", {score_errors} scoring errors" if score_errors else "")
            + (f", {st.reconnects} reconnects" if st.reconnects else "")
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
