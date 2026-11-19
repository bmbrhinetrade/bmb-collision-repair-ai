"""The ceoscan service: one process that ingests, scores, alerts and reports on itself.

``Service.run`` wires every module together:

* X ingest - the filtered stream (rules are synced first) with gap recovery through timeline
  polling after an outage, or timeline polling when the plan has no filtered stream. In
  ``x_api.mode: auto`` a stream that turns out to be unavailable falls back to polling
  automatically and the resulting latency is printed. After a restart, posts made while ceoscan
  was not running are fetched once (``x_api.recovery.on_startup``), never older than
  ``x_api.recovery.max_age_seconds``;
* the pipeline workers (fast rules -> Claude -> decision -> push), ``ops.pipeline_workers`` of them;
* the health server (``GET /health``) and the ops scheduler (stream-down alerts, daily summary);
* the optional Claude analyst desk (``claude_desk``: a Claude Code scheduled task fired for strong
  alerts, plus a daily review), sharing the HTTP session; stopped after the pipeline has drained;
* housekeeping: the "last alive" time (kv ``ingest_last_alive``, every ~30 s and at shutdown) that
  the next start catches up from, and a throttled alert when Claude rejects our requests.

The "ceoscan started" alert is sent once ingest is really up (rules synced and the stream
connected, or the first successful poll). A configuration problem only the user can fix sends
one "ceoscan stopped: configuration problem: ..." alert (not repeated by restart loops: kv
``config_problem_alert`` holds a hash of the message), then

* ``ops.config_error_action: exit`` (default): exit with code 2 (systemd's
  ``RestartPreventExitStatus=2`` keeps it from restart-looping), or
* ``wait`` (the Docker image; env ``CEOSCAN_CONFIG_ERROR_ACTION``): stay up idle with /health
  reporting state ``config_error`` until stopped, so a container restart policy cannot loop.

SIGINT / SIGTERM stop everything gracefully (a second signal forces it). Exit codes: 0 after a
requested shutdown, 1 when a component crashed, 2 for a configuration problem.

ALERTS ONLY: nothing in this package trades or connects to a brokerage.
"""

from __future__ import annotations

import asyncio
import contextlib
import functools
import hashlib
import json
import logging
import signal
import sqlite3
import time
from collections.abc import Awaitable, Callable, Coroutine
from datetime import datetime
from pathlib import Path
from typing import Any, TypeVar

import aiohttp

from .http import new_session
from . import __version__
from .alerts.claude_desk import ClaudeDesk, build_desk
from .alerts.dispatcher import AlertDispatcher, build_dispatcher
from .alerts.format import format_system_alert
from .config import AppConfig
from .logging_setup import setup_logging
from .models import AlertRecord, IngestStatus, Priority
from .ops.cost import estimate_monthly_cost, format_cost_estimate
from .ops.health import HealthServer
from .ops.scheduler import OpsScheduler
from .pipeline import Pipeline
from .scoring.claude import ClaudeScorer, OfflineScorer
from .store import Store
from .timeutil import now_utc
from .xapi.access import STREAM_LATENCY_S, AccessReport, check_access, format_access_report
from .xapi.client import CreditsDepleted, NotAuthorized, RateLimited, TooManyConnections, XApiError, XClient
from .xapi.poller import TimelinePoller, compute_poll_interval, estimate_poll_latency
from .xapi.rules import RuleBuildResult, RuleSpec, build_rules, sync_rules
from .xapi.stream import Backoff, FilteredStream, IngestEventLog, StreamUnavailable

logger = logging.getLogger(__name__)

T = TypeVar("T")

EXIT_OK = 0
EXIT_ERROR = 1
EXIT_CONFIG = 2

#: Printed (and logged) at startup.
ALERTS_ONLY_BANNER = (
    f"ceoscan {__version__} - ALERTS ONLY: watches X posts and sends alerts to your phone. "
    "It never places trades and never connects to a brokerage."
)

MISSING_X_TOKEN = (
    "X_BEARER_TOKEN is not set. Put the Bearer Token of your X app (console.x.com -> your app -> "
    "keys and tokens) in .env (see .env.example) or in the environment."
)

NO_VERIFIED_ACCOUNTS = (
    "no watchlist account has a verified user_id, so there is nothing to watch. "
    "Run `python -m ceoscan verify --write` to look the handles up on X and store their user_ids."
)

#: How long a graceful shutdown may take before the remaining tasks are cancelled
#: (systemd TimeoutStopSec / compose stop_grace_period are 20 s).
SHUTDOWN_TIMEOUT_SECONDS = 15.0
#: Floor for retrying a failed stream-rule sync after a network error (never spin).
MIN_RULE_SYNC_RETRY_SECONDS = 1.0

#: kv: when ingest last had coverage (ISO time). The next start catches up from here.
KV_LAST_ALIVE = "ingest_last_alive"
#: kv: {"hash", "at"} of the last configuration-problem alert (restart loops must not spam).
KV_CONFIG_ALERT = "config_problem_alert"
#: How often the housekeeping loop runs (persists the last-alive time, checks Claude rejections).
HOUSEKEEPING_INTERVAL_SECONDS = 30.0
#: The same configuration problem is alerted again only after this long ...
CONFIG_ALERT_REPEAT_SECONDS = 24 * 3600.0
#: ... or once ingest has been up this long (the problem was fixed; a recurrence alerts again).
CONFIG_ALERT_RESET_AFTER_UP_SECONDS = 300.0
#: The same Claude rejection is alerted at most once per this many seconds.
CLAUDE_REJECTION_ALERT_SECONDS = 3600.0
#: A polling interval change by at least this factor (either way) also sends a system alert.
PACING_ALERT_RATIO = 1.5
CONFIG_ALERT_TITLE = "ceoscan stopped: configuration problem"


class ConfigProblem(Exception):
    """A configuration problem only the user can fix (exit code 2 or idle, per ops.config_error_action)."""


def _describe_exc(exc: BaseException) -> str:
    """Exception type and message on one line (X errors never carry secrets)."""
    text = " ".join(str(exc).split())
    return f"{type(exc).__name__}: {text}"[:300] if text else type(exc).__name__


def describe_latency(latency: tuple[float, float]) -> str:
    return f"typical {latency[0]:.1f}s, worst {latency[1]:.1f}s"


def _parse_time(value: Any) -> datetime | None:
    try:
        dt = datetime.fromisoformat(str(value))
    except (TypeError, ValueError):
        return None
    return dt if dt.tzinfo is not None else None


class Service:
    """The long-running ceoscan service (``python -m ceoscan run``)."""

    def __init__(
        self,
        cfg: AppConfig,
        *,
        out: Callable[[str], Any] = print,
        install_signal_handlers: bool = True,
        shutdown_timeout: float = SHUTDOWN_TIMEOUT_SECONDS,
    ) -> None:
        self.cfg = cfg
        self.settings = cfg.settings
        self.out = out
        self.install_signal_handlers = install_signal_handlers
        self.shutdown_timeout = shutdown_timeout
        #: set when the components must stop (shutdown requested, a crash, or a configuration problem)
        self.stop = asyncio.Event()
        #: set only when a shutdown was requested (signal / request_stop)
        self._shutdown = asyncio.Event()
        #: set once ingest is really up: stream connected after the rule sync, or a poll succeeded
        self._ingest_up = asyncio.Event()
        self._ingest_up_at: float | None = None  # time.monotonic()
        self.status = IngestStatus()
        self.started_at = now_utc()
        self.mode: str | None = None  # "stream" | "poll" once decided
        self.access_report: AccessReport | None = None
        # Set while running (for introspection and tests).
        self.store: Store | None = None
        self.dispatcher: AlertDispatcher | None = None
        self.health: HealthServer | None = None
        self.poller: TimelinePoller | None = None
        self.stream: FilteredStream | None = None
        self.scorer: ClaudeScorer | OfflineScorer | None = None
        self.events: IngestEventLog | None = None
        self.desk: ClaudeDesk | None = None
        self.desk_problem: str | None = None  # why claude_desk.enabled is true but the desk is off
        self.config_problem: str | None = None
        self.warmup_error: str | None = None
        self._exit_code = EXIT_OK
        self._tasks: set[asyncio.Task[Any]] = set()
        self._background: set[asyncio.Task[Any]] = set()
        self._signals_installed: list[signal.Signals] = []
        self._last_alive_written: datetime | None = None
        self._config_alert_reset = False
        self._claude_alerted: tuple[str, float] | None = None

    def __repr__(self) -> str:  # never show the config's secrets
        return f"Service(mode={self.mode!r}, state={self.status.state!r})"

    # ------------------------------------------------------------------ public API

    def request_stop(self) -> None:
        """Ask the service to shut down gracefully (what SIGINT/SIGTERM do)."""
        self._shutdown.set()
        self.stop.set()

    async def run(self) -> int:
        """Run until stopped. Returns the process exit code (0 ok, 1 crash, 2 configuration)."""
        setup_logging(self.settings.ops.log_level, self.cfg.secrets.secret_values())
        self.out(ALERTS_ONLY_BANNER)
        logger.info("starting ceoscan %s (alerts only - never trades)", __version__)
        try:
            return await self._run()
        except ConfigProblem as exc:  # only before the store / alert channels existed
            self._config_error(str(exc))
            return EXIT_CONFIG
        except Exception:
            logger.exception("ceoscan crashed")
            return EXIT_ERROR
        finally:
            self._remove_signal_handlers()
            self.status.state = "stopped"

    # ------------------------------------------------------------------ startup

    def _config_error(self, message: str) -> None:
        logger.error("configuration problem: %s", message)

    def _build_rules(self) -> RuleBuildResult:
        """Build the stream rules; fail with ConfigProblem when there is nothing to watch."""
        wl = self.cfg.watchlist
        try:
            build = build_rules(wl, self.settings.x_api.rules)
        except ValueError as exc:
            raise ConfigProblem(f"cannot build the filtered-stream rules: {exc}") from exc
        for warning in build.warnings:  # includes the skipped (unverified) accounts
            logger.warning("rules: %s", warning)
        if not any(a.user_id for a in wl.accounts):
            raise ConfigProblem(NO_VERIFIED_ACCOUNTS)
        return build

    def _make_scorer(self) -> ClaudeScorer | OfflineScorer:
        sc = self.settings.scoring
        key = self.cfg.secrets.anthropic_api_key
        if sc.enabled and key:
            logger.info(
                "scoring: Claude %s (effort %s, budget %.1fs per post)", sc.model, sc.effort, sc.total_budget_seconds
            )
            return ClaudeScorer(sc, key)
        if sc.enabled:
            logger.warning(
                "ANTHROPIC_API_KEY is not set - posts are scored by the fast rules only (noisier alerts)"
            )
        else:
            logger.warning("scoring.enabled is false - posts are scored by the fast rules only")
        return OfflineScorer(sc)

    def _open_store(self) -> tuple[Store, str | None]:
        """The database, or an in-memory stand-in plus the problem when it cannot be opened.

        The stand-in keeps /health and the configuration-problem alert working.
        """
        path = self.settings.ops.db_path
        try:
            return Store(path), None
        except (OSError, sqlite3.Error) as exc:
            problem = f"cannot open the database {path!r} (ops.db_path): {_describe_exc(exc)}"
        try:
            return Store(":memory:"), problem
        except (OSError, sqlite3.Error) as exc:  # pragma: no cover - sqlite itself is broken
            raise ConfigProblem(problem) from exc

    def _fallback_access_report(self, n_accounts: int, problem: str) -> AccessReport:
        """Used when the access check itself failed unexpectedly: trust the configured mode."""
        xs = self.settings.x_api
        mode = "poll" if xs.mode == "poll" else "stream"
        interval = compute_poll_interval(n_accounts, xs.poll) if n_accounts else None
        latency = STREAM_LATENCY_S if mode == "stream" else estimate_poll_latency(interval or 0.0)
        return AccessReport(
            mode=mode,
            stream_available=None,
            credits_ok=None,
            usage=None,
            existing_rules=None,
            poll_interval_s=interval,
            est_latency_s=latency,
            messages=[f"access check failed ({problem}); using mode {mode}"],
        )

    async def _check_access(
        self, client: XClient, session: aiohttp.ClientSession, token: str, n_accounts: int
    ) -> AccessReport | None:
        try:
            return await self._until_stop(
                check_access(client, self.settings.x_api, n_accounts, session, token)
            )
        except Exception as exc:  # the check must never keep the service from starting
            logger.exception("access check failed")
            return self._fallback_access_report(n_accounts, _describe_exc(exc))

    async def _until_stop(self, aw: Awaitable[T]) -> T | None:
        """Await ``aw`` unless a stop is requested first (then cancel it and return None)."""
        task: asyncio.Future[T] = asyncio.ensure_future(aw)
        stopper: asyncio.Future[Any] = asyncio.create_task(self.stop.wait())
        waiting: set[asyncio.Future[Any]] = {task, stopper}
        try:
            await asyncio.wait(waiting, return_when=asyncio.FIRST_COMPLETED)
        except asyncio.CancelledError:
            task.cancel()
            raise
        finally:
            stopper.cancel()
        if task.done():
            return task.result()
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task
        return None

    def _read_last_alive(self, store: Store) -> datetime | None:
        """When ingest last had coverage before this start (kv), or None."""
        try:
            value = store.kv_get(KV_LAST_ALIVE)
        except Exception:
            logger.debug("could not read %s", KV_LAST_ALIVE, exc_info=True)
            return None
        dt = _parse_time(value) if value else None
        if dt is not None and dt > self.started_at:
            return None  # clock went backwards; do not trust it
        return dt

    # ------------------------------------------------------------------ main body

    def _preflight(self) -> RuleBuildResult:
        """Checks that need no I/O: the X token and the stream rules. Raises ConfigProblem."""
        if not self.cfg.secrets.x_bearer_token:
            raise ConfigProblem(MISSING_X_TOKEN)
        return self._build_rules()

    async def _run(self) -> int:
        s = self.settings
        self._install_signal_handlers()
        build: RuleBuildResult | None = None
        early_problem: str | None = None
        try:
            build = self._preflight()
        except ConfigProblem as exc:
            early_problem = str(exc)

        async with contextlib.AsyncExitStack() as stack:
            if early_problem is None or Path(s.ops.db_path).exists():
                # an existing database also dedupes the problem alert across restarts
                store, store_problem = self._open_store()
            else:  # nothing to run: create nothing on disk, keep /health and the alert working
                store, store_problem = Store(":memory:"), None
            stack.callback(store.close)
            self.store = store
            session = await stack.enter_async_context(new_session())
            dispatcher = build_dispatcher(self.cfg, session)  # warns (names only) about missing channel secrets
            self.dispatcher = dispatcher
            queue: asyncio.Queue = asyncio.Queue(maxsize=max(0, int(s.ops.queue_max)))

            if early_problem is None or s.ops.config_error_action == "wait":
                await self._start_health(stack, store, queue)

            code = EXIT_OK
            try:
                problem = early_problem or store_problem
                if problem is not None or build is None:
                    raise ConfigProblem(problem or "invalid configuration")
                code = await self._run_service(stack, store, session, queue, build)
            except ConfigProblem as exc:
                self.config_problem = self.config_problem or str(exc)
            if self.config_problem is not None:
                return await self._handle_config_problem(self.config_problem)
            return code

    async def _start_health(self, stack: contextlib.AsyncExitStack, store: Store, queue: asyncio.Queue) -> None:
        s = self.settings
        health = HealthServer(s.ops.health_host, s.ops.health_port, self.status, store, s, self.started_at, queue)
        try:
            await health.start()
            stack.push_async_callback(health.stop)
            self.health = health
        except OSError as exc:  # alerting matters more than the health endpoint
            logger.error(
                "health server could not listen on %s:%s (%s); continuing without it",
                s.ops.health_host,
                s.ops.health_port,
                _describe_exc(exc),
            )
            store.log_event("health_error", _describe_exc(exc), level="error")

    async def _run_service(
        self,
        stack: contextlib.AsyncExitStack,
        store: Store,
        session: aiohttp.ClientSession,
        queue: asyncio.Queue,
        build: RuleBuildResult,
    ) -> int:
        cfg, s = self.cfg, self.settings
        token = cfg.secrets.x_bearer_token
        assert token  # checked by _preflight
        verified = [a for a in cfg.watchlist.accounts if a.user_id]
        assert self.dispatcher is not None

        scorer = self._make_scorer()
        stack.push_async_callback(scorer.aclose)
        self.scorer = scorer

        client = XClient(session, token, s.x_api)
        report = await self._check_access(client, session, token, len(verified))
        if report is None or self.stop.is_set():
            logger.info("stop requested during startup")
            return self._exit_code
        self.access_report = report
        self.out(format_access_report(report))

        self.events = IngestEventLog(store)
        rec = s.x_api.recovery
        last_alive = self._read_last_alive(store)
        poller = TimelinePoller(
            client,
            cfg.watchlist,
            s.x_api,
            queue,
            self.status,
            store,
            catch_up_since=last_alive if rec.enabled and rec.on_startup else None,
            events=self.events,
            on_up=self._on_ingest_up,
            on_pacing_change=self._on_pacing_change,
        )
        self.poller = poller
        mode = report.mode
        if mode == "poll":
            forced = s.x_api.mode == "poll"
            self._announce_polling(poller, None if forced else "the access check found no filtered-stream access")
        self.mode = mode
        self.status.mode = "poll" if mode == "poll" else "stream"
        self.status.estimated_latency_seconds = (
            report.est_latency_s[0] if mode == "stream" else poller.latency[0]
        )
        self.out(format_cost_estimate(estimate_monthly_cost(cfg.watchlist, s, mode=mode)))

        self._spawn_background(self._warmup_scorer(scorer), "scorer-warmup")
        self._spawn_background(self._startup_alert(report, build, len(verified)), "startup-alert")
        self._spawn_background(self._housekeeping(), "housekeeping")

        desk = self._start_desk(stack, store, session)
        pipeline = Pipeline(cfg, store, scorer, self.dispatcher, self.status, desk=desk)
        scheduler = OpsScheduler(cfg, store, self.status, self.dispatcher, desk=desk)
        return await self._supervise(
            {
                "ingest": self._ingest(mode, client, session, token, build.rules, poller, queue, last_alive),
                "pipeline": pipeline.run(queue, self.stop, workers=s.ops.pipeline_workers),
                "scheduler": scheduler.run(self.stop),
            }
        )

    def _start_desk(
        self, stack: contextlib.AsyncExitStack, store: Store, session: aiohttp.ClientSession
    ) -> ClaudeDesk | None:
        """The Claude analyst desk when enabled and configured; otherwise None (one warning, never fatal)."""
        try:
            desk, problem = build_desk(self.cfg, store, session, self.dispatcher)
        except Exception as exc:  # the desk is optional: never keep the scanner from running
            logger.exception("claude_desk: could not be set up; continuing without it")
            desk, problem = None, f"could not be set up: {_describe_exc(exc)}"
        self.desk, self.desk_problem = desk, problem
        if self.health is not None:
            self.health.desk, self.health.desk_problem = desk, problem
        if desk is None:
            if problem:
                self.out(f"WARNING: the Claude analyst desk is off: {problem}. Phone alerts are not affected.")
            return None
        desk.start()
        stack.push_async_callback(desk.stop)  # runs after the pipeline drained (it may still submit)
        d = self.settings.claude_desk
        self.out(
            f"Claude analyst desk: on (alerts with score >= {d.min_score}"
            + (" or from always-alert accounts" if d.include_always_alert else "")
            + f", at most {d.max_fires_per_hour} runs/hour"
            + (", daily review" if d.daily_review else "")
            + ")"
        )
        return desk

    # ------------------------------------------------------------------ ingest

    async def _ingest(
        self,
        mode: str,
        client: XClient,
        session: aiohttp.ClientSession,
        token: str,
        rules: list[RuleSpec],
        poller: TimelinePoller,
        queue: asyncio.Queue,
        last_alive: datetime | None = None,
    ) -> None:
        xs = self.settings.x_api
        rec = xs.recovery
        if mode == "stream":
            try:
                if not await self._sync_rules_until_done(client, rules):
                    return  # stop requested
                startup_catch_up = rec.enabled and rec.on_startup
                if startup_catch_up:
                    # Before the first connect: live posts must not move the cursors past posts
                    # made while ceoscan was not running.
                    poller.snapshot_cursors()
                stream = FilteredStream(
                    session,
                    token,
                    xs,
                    queue,
                    self.status,
                    client=client,
                    on_reconnect_gap=self._gap_recovery(poller),
                    on_gap_start=poller.snapshot_cursors if rec.enabled else None,
                    on_connected=self._on_ingest_up,
                    catch_up_on_first_connect=startup_catch_up,
                    gap_started_at=last_alive if startup_catch_up else None,
                    events=self.events,
                )
                self.stream = stream
                logger.info("ingest: filtered stream (%d rules)", len(rules))
                await stream.run(self.stop)
                return
            except StreamUnavailable as exc:
                if xs.mode != "auto":
                    raise ConfigProblem(
                        f"{exc}. x_api.mode is '{xs.mode}'; set it to 'auto' (fall back automatically) "
                        "or 'poll' in the settings file"
                    ) from exc
                self.mode = "poll"
                self._announce_polling(poller, str(exc))
                self._spawn_background(self._fallback_alert(poller, str(exc)), "fallback-alert")
        logger.info("ingest: timeline polling")
        await poller.run(self.stop)

    async def _sync_rules_until_done(self, client: XClient, rules: list[RuleSpec]) -> bool:
        """Sync the stream rules, retrying transient failures. False when a stop was requested.

        Rejected rules raise ConfigProblem; a 403 from the rules endpoint means this app has no
        filtered stream (StreamUnavailable).
        """
        rs = self.settings.x_api.rules
        backoff = Backoff()
        st = self.status
        st.mode = "stream"
        while not self.stop.is_set():
            reset_at = None
            try:
                plan = await self._until_stop(sync_rules(client, rules, rs, apply=True))
            except ValueError as exc:
                raise ConfigProblem(f"{exc}. Fix the watchlist (accounts / keyword_rules) and restart") from exc
            except NotAuthorized as exc:
                if exc.status == 403:
                    raise StreamUnavailable(f"stream rules endpoint forbidden: {exc}") from exc
                logger.error("rules: X rejected the credentials (%s); check X_BEARER_TOKEN", exc)
                kind, error = "http", str(exc)
            except CreditsDepleted as exc:
                kind, error = "credits", str(exc)
            except (RateLimited, TooManyConnections) as exc:
                kind, error = "rate_limit", str(exc)
                reset_at = exc.rate_limit.reset_at if exc.rate_limit else None
            except XApiError as exc:
                kind, error = "http", str(exc)
            except (aiohttp.ClientError, OSError, TimeoutError) as exc:
                kind, error = "network", _describe_exc(exc)
            else:
                if plan is None:
                    return False
                logger.info(
                    "rules: in sync (%d unchanged, %d added, %d deleted, %d foreign kept)",
                    len(plan.unchanged),
                    len(plan.to_add),
                    len(plan.to_delete),
                    0 if rs.manage_foreign_rules else len(plan.foreign),
                )
                return True

            if st.down_since is None:
                st.down_since = now_utc()
            st.last_error = f"stream rule sync failed: {error}"[:300]
            if self.events is not None:
                self.events.error("stream_error", st.last_error)
            if kind == "credits":
                st.state = "credits_depleted"
                delay = float(self.settings.x_api.stream.credits_depleted_retry_seconds)
                logger.error("rules: X API credits depleted - add credits at console.x.com; retry in %.0fs", delay)
            else:
                st.state = "backoff"
                delay = backoff.next_delay(kind, reset_at=reset_at)  # type: ignore[arg-type]
                if kind == "network":
                    delay = max(delay, MIN_RULE_SYNC_RETRY_SECONDS)
                logger.warning("rules: sync failed (%s); retry in %.1fs", error, delay)
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self.stop.wait(), timeout=delay)
        return False

    def _gap_recovery(self, poller: TimelinePoller) -> Callable[[float], Awaitable[None]]:
        """The stream's on_reconnect_gap callback: fetch what was posted during the outage."""

        async def recover(gap_seconds: float) -> None:
            logger.info(
                "recovery: %.0fs without stream coverage; fetching missed posts from timelines", gap_seconds
            )
            n = await poller.recover()
            if n is not None and self.store is not None:  # None: merged into a running recovery
                self.store.log_event("gap_recovery", f"gap {gap_seconds:.0f}s, {n} post(s) recovered")

        return recover

    def _on_ingest_up(self) -> None:
        """Ingest delivers data: the stream connected or a poll succeeded."""
        if not self._ingest_up.is_set():
            self._ingest_up_at = time.monotonic()
            self._ingest_up.set()

    def _announce_polling(self, poller: TimelinePoller, why: str | None) -> None:
        """Print and log the polling cadence and the resulting latency."""
        line = (
            f"Timeline polling: {len(poller.accounts)} account(s), each checked every {poller.interval:.1f}s; "
            f"est. latency {describe_latency(poller.latency)} "
            f"(filtered stream: {describe_latency(STREAM_LATENCY_S)})"
        )
        if why:
            line = f"Filtered stream unavailable ({why}); falling back to timeline polling.\n{line}"
        self.out(line)
        logger.warning("%s", " ".join(line.split()))
        n_kw = len(self.cfg.watchlist.keyword_rules)
        if n_kw:
            note = f"{n_kw} keyword rule(s) need the filtered stream and are not active while polling"
            self.out(f"WARNING: {note}.")
            logger.warning("poll: %s", note)
        self._set_polling_note(poller)

    def _set_polling_note(self, poller: TimelinePoller) -> None:
        notes = [n for n in self.status.notes if not n.startswith("polling:")]
        notes.append(f"polling: every {poller.interval:.1f}s, est. latency {poller.latency[0]:.1f}s")
        self.status.notes[:] = notes

    def _on_pacing_change(self, old_interval: float, new_interval: float) -> None:
        """X's real timeline rate limit changed the polling interval: tell the user the new latency."""
        poller = self.poller
        if poller is None:
            return
        line = (
            f"Timeline polling re-paced to X's real rate limit ({poller.rate_limit:,} requests / 15 min): "
            f"{len(poller.accounts)} account(s), each checked every {new_interval:.1f}s "
            f"(was {old_interval:.1f}s); est. latency {describe_latency(poller.latency)}"
        )
        self.out(line)
        logger.warning("%s", line)
        self._set_polling_note(poller)
        ratio = new_interval / old_interval if old_interval > 0 else float("inf")
        if ratio >= PACING_ALERT_RATIO or ratio <= 1 / PACING_ALERT_RATIO:
            body = (
                f"X allows {poller.rate_limit:,} timeline requests per 15 min for this app "
                f"(settings assumed {self.settings.x_api.poll.max_requests_per_15min:,}).\n"
                f"Each of {len(poller.accounts)} account(s) is now checked every {new_interval:.1f}s; "
                f"est. latency {describe_latency(poller.latency)}."
            )
            self._spawn_background(
                self._send_system(f"ceoscan polling latency now {describe_latency(poller.latency)}", body, "poll_pacing"),
                "pacing-alert",
            )

    # ------------------------------------------------------------------ system alerts

    async def _send_system(self, title: str, body: str, reason: str, priority: Priority = "normal") -> None:
        if self.dispatcher is None or self.store is None:
            return
        msg = format_system_alert(title, body, priority=priority)
        results = await self.dispatcher.send(msg)
        rec = AlertRecord(post_id=None, kind="system", decision_reason=reason, message=msg, results=results)
        try:
            self.store.record_alert(rec)
            self.store.log_event(reason, title)
        except Exception:  # bookkeeping must never break alerting
            logger.exception("could not record the %s alert", reason)
        if results and not rec.sent_ok:
            logger.warning("system alert %r was not delivered by any channel", title)

    async def _startup_alert(self, report: AccessReport, build: RuleBuildResult, n_verified: int) -> None:
        """Sent once ingest is really up (not when the start fails on rules, credits or the network)."""
        if await self._until_stop(self._ingest_up.wait()) is None and not self._ingest_up.is_set():
            return  # stopped before ingest came up
        poller = self.poller
        mode = self.mode or report.mode
        latency = report.est_latency_s if mode == "stream" or poller is None else poller.latency
        title = f"ceoscan started ({mode}, {n_verified} accounts, est. {latency[0]:.1f}s)"
        lines = [
            "Mode: filtered stream"
            if mode == "stream" or poller is None
            else f"Mode: timeline polling (each account every {poller.interval:.1f}s)",
            f"Accounts: {n_verified} verified"
            + (f", {len(build.skipped_accounts)} skipped (no user_id)" if build.skipped_accounts else ""),
            f"Est. latency: {describe_latency(latency)}",
        ]
        if mode == "stream" and self.cfg.watchlist.keyword_rules:
            lines.append(f"Keyword rules: {len(self.cfg.watchlist.keyword_rules)}")
        if self.warmup_error:
            lines.append(f"Claude: {self.warmup_error}")
        lines.append("Alerts only - ceoscan never trades.")
        await self._send_system(title, "\n".join(lines), "startup")

    async def _fallback_alert(self, poller: TimelinePoller, why: str) -> None:
        body = (
            f"The filtered stream is not available for this X app ({why}).\n"
            f"Polling {len(poller.accounts)} account(s), each every {poller.interval:.1f}s; "
            f"est. latency {describe_latency(poller.latency)}."
        )
        await self._send_system("ceoscan switched to timeline polling", body, "poll_fallback")

    # ------------------------------------------------------------------ Claude warm-up

    async def _warmup_scorer(self, scorer: ClaudeScorer | OfflineScorer) -> None:
        """One tiny Claude request at startup: validates key/model/parameters, warms TLS. Never exits."""
        warmup = getattr(scorer, "warmup", None)
        if warmup is None:
            return
        error = await warmup()
        if isinstance(scorer, OfflineScorer):
            return  # rules-only scoring: nothing to check (already logged at startup)
        if error is None:
            self.out(f"Claude scoring: OK ({self.settings.scoring.model})")
            return
        self.warmup_error = error
        if getattr(scorer, "warmup_transient", None) is False:
            line = (
                f"WARNING: {error}. Until this is fixed every post is scored by the fast rules only "
                f"(rules-only alerts, impact capped at {self.settings.scoring.fallback_max_impact})."
            )
            self.out(line)
            logger.error("%s", line)
            # keyed like the runtime check in _housekeeping (scorer.last_rejection): one alert, not two
            await self._alert_claude_rejection(str(getattr(scorer, "last_rejection", None) or error), error)
        else:
            line = f"Note: {error}; Claude is tried again for every post (fast-rules score if it fails)."
            self.out(line)
            logger.warning("%s", line)

    async def _alert_claude_rejection(self, key: str, error: str | None = None) -> None:
        """System alert when Claude rejects our requests (bad key / model / billing), at most hourly
        per distinct rejection (``key``)."""
        error = error or key
        now = time.monotonic()
        prev = self._claude_alerted
        if prev is not None and prev[0] == key and now - prev[1] < CLAUDE_REJECTION_ALERT_SECONDS:
            return
        self._claude_alerted = (key, now)
        if self.store is not None:
            with contextlib.suppress(Exception):
                self.store.log_event("claude_rejected", error, level="error")
        body = (
            f"{error}\nceoscan keeps running: posts are scored by the fast rules only (alerts labelled "
            f"rules-only, impact capped at {self.settings.scoring.fallback_max_impact}) until Claude accepts "
            "requests again."
        )
        await self._send_system("Claude scoring unavailable - rules-only alerts", body, "claude_rejected", "high")

    # ------------------------------------------------------------------ housekeeping

    async def _housekeeping(self) -> None:
        """Every ~30 s: persist the last-alive time, alert on Claude rejections, reset the
        configuration-alert dedupe once ingest has been up for a while. Never raises."""
        while True:
            self._persist_last_alive()
            self._maybe_reset_config_alert()
            rejection = getattr(self.scorer, "last_rejection", None)
            if rejection:
                try:
                    await self._alert_claude_rejection(
                        str(rejection), f"Claude rejected a scoring request ({rejection})"
                    )
                except Exception:
                    logger.exception("housekeeping: Claude rejection alert failed")
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self.stop.wait(), timeout=HOUSEKEEPING_INTERVAL_SECONDS)
                break
        self._persist_last_alive()

    def _persist_last_alive(self) -> None:
        """kv ``ingest_last_alive`` = the last moment ingest had coverage (last bytes / poll)."""
        at = self.status.last_heartbeat_at
        store = self.store
        if at is None or store is None or not self._ingest_up.is_set():
            return
        if self._last_alive_written is not None and at <= self._last_alive_written:
            return
        try:
            store.kv_set(KV_LAST_ALIVE, at.isoformat())
            self._last_alive_written = at
        except Exception:  # bookkeeping must never break the service
            logger.debug("could not persist %s", KV_LAST_ALIVE, exc_info=True)

    def _maybe_reset_config_alert(self) -> None:
        if self._config_alert_reset or self._ingest_up_at is None or self.store is None:
            return
        if time.monotonic() - self._ingest_up_at < CONFIG_ALERT_RESET_AFTER_UP_SECONDS:
            return
        self._config_alert_reset = True
        try:
            if self.store.kv_get(KV_CONFIG_ALERT):
                self.store.kv_set(KV_CONFIG_ALERT, "")
        except Exception:
            logger.debug("could not reset %s", KV_CONFIG_ALERT, exc_info=True)

    # ------------------------------------------------------------------ configuration problems

    async def _handle_config_problem(self, message: str) -> int:
        """Alert once, then exit (code 2) or stay up idle, per ops.config_error_action."""
        self._config_error(message)
        if any(not t.done() for t in (*self._tasks, *self._background)):
            self.stop.set()
            await self._drain()
        st = self.status
        st.state = "config_error"
        st.last_error = f"configuration problem: {message}"[:300]
        st.connected_since = None
        if st.down_since is None:
            st.down_since = now_utc()
        action = self.settings.ops.config_error_action
        try:
            await self._config_problem_alert(message, action)
        except Exception:  # the alert is best effort; the exit / idle behaviour is not
            logger.exception("could not send the configuration-problem alert")
        if action == "wait" and not self._shutdown.is_set():
            note = (
                "ceoscan is idle because of this configuration problem (ops.config_error_action: wait); "
                "/health reports state config_error. Fix the configuration and restart ceoscan."
            )
            self.out(f"ERROR: configuration problem: {message}\n{note}")
            logger.error("%s", note)
            await self._shutdown.wait()
        return EXIT_CONFIG

    async def _config_problem_alert(self, message: str, action: str) -> None:
        """At most one alert per distinct problem (kv hash), so a restart loop cannot spam the phone."""
        store = self.store
        if store is None:
            return
        digest = hashlib.sha256(message.encode("utf-8", errors="replace")).hexdigest()[:16]
        now = now_utc()
        try:
            raw = store.kv_get(KV_CONFIG_ALERT)
            prev = json.loads(raw) if raw else None
        except Exception:
            prev = None
        if isinstance(prev, dict) and prev.get("hash") == digest:
            at = _parse_time(prev.get("at"))
            if at is not None and 0 <= (now - at).total_seconds() < CONFIG_ALERT_REPEAT_SECONDS:
                logger.info("configuration problem alert already sent at %s; not repeating it", at.isoformat())
                return
        try:  # before sending: even a crash during the send must not cause a repeat
            store.kv_set(KV_CONFIG_ALERT, json.dumps({"hash": digest, "at": now.isoformat()}))
        except Exception:
            logger.warning("could not record the configuration-problem alert; it may be repeated")
        short = " ".join(message.split())
        if len(short) > 120:
            short = short[:117].rstrip() + "..."
        if action == "wait":
            what = "It stays up idle (/health: state config_error) - fix the configuration and restart it."
        else:
            what = "It exited (code 2) and will not restart by itself - fix the configuration and start it again."
        body = f"{message}\nceoscan is not watching X. {what}"
        await self._send_system(f"{CONFIG_ALERT_TITLE}: {short}", body, "config_problem", priority="high")

    # ------------------------------------------------------------------ supervision

    def _spawn_background(self, coro: Coroutine[Any, Any, None], name: str) -> None:
        task = asyncio.create_task(coro, name=f"ceoscan-{name}")
        self._background.add(task)

        def done(t: asyncio.Task[Any]) -> None:
            self._background.discard(t)
            if not t.cancelled() and t.exception() is not None:
                logger.error("%s failed: %s", name, _describe_exc(t.exception()))  # type: ignore[arg-type]

        task.add_done_callback(done)

    def _fail(self, code: int) -> None:
        if self._exit_code == EXIT_OK:
            self._exit_code = code
        self.stop.set()

    def _on_task_done(self, name: str, task: asyncio.Task[Any]) -> None:
        if task.cancelled():
            return
        exc = task.exception()
        if isinstance(exc, ConfigProblem):
            if self.config_problem is None:
                self.config_problem = str(exc)
            self._fail(EXIT_CONFIG)
        elif exc is not None:
            logger.error("%s crashed: %s", name, _describe_exc(exc), exc_info=exc)
            self._fail(EXIT_ERROR)
        elif not self.stop.is_set():
            logger.error("%s stopped unexpectedly; shutting down", name)
            self._fail(EXIT_ERROR)

    async def _supervise(self, coros: dict[str, Coroutine[Any, Any, None]]) -> int:
        """Run the components until a stop is requested or one of them fails, then shut down."""
        for name, coro in coros.items():
            task = asyncio.create_task(coro, name=f"ceoscan-{name}")
            task.add_done_callback(functools.partial(self._on_task_done, name))
            self._tasks.add(task)
        logger.info("ceoscan running (mode %s); press Ctrl-C to stop", self.mode)
        try:
            await self.stop.wait()
            if self.config_problem is None:
                logger.info("shutting down")
        finally:
            self.stop.set()
            self._persist_last_alive()
            await self._drain()
            self._persist_last_alive()
        return self._exit_code

    async def _drain(self) -> None:
        tasks = [*self._tasks, *self._background]
        pending = {t for t in tasks if not t.done()}
        if pending:
            _, pending = await asyncio.wait(pending, timeout=self.shutdown_timeout)
        if pending:
            names = ", ".join(sorted(t.get_name() for t in pending))
            logger.warning("shutdown: %s did not stop within %.0fs; cancelling", names, self.shutdown_timeout)
            for t in pending:
                t.cancel()
            await asyncio.gather(*pending, return_exceptions=True)
        logger.info("ceoscan stopped" if self.config_problem is None else "ceoscan components stopped")

    # ------------------------------------------------------------------ signals

    def _on_signal(self, signame: str) -> None:
        if not self._shutdown.is_set():
            logger.info("received %s; shutting down gracefully (send it again to force)", signame)
            self.request_stop()
            return
        logger.warning("received %s again; forcing shutdown", signame)
        for t in [*self._tasks, *self._background]:
            t.cancel()

    def _on_os_signal(self, loop: asyncio.AbstractEventLoop, signum: int, _frame: Any) -> None:
        """Fallback handler (no loop.add_signal_handler): hand the signal over to the loop."""
        loop.call_soon_threadsafe(self._on_signal, signal.Signals(signum).name)

    def _install_signal_handlers(self) -> None:
        if not self.install_signal_handlers:
            return
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                loop.add_signal_handler(sig, self._on_signal, sig.name)
            except (NotImplementedError, RuntimeError, ValueError):  # e.g. Windows, non-main thread
                try:
                    signal.signal(sig, functools.partial(self._on_os_signal, loop))
                except (ValueError, OSError):
                    logger.warning("could not install a %s handler", sig.name)
                    continue
            self._signals_installed.append(sig)

    def _remove_signal_handlers(self) -> None:
        if not self._signals_installed:
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:  # pragma: no cover - always called inside the loop
            return
        for sig in self._signals_installed:
            try:
                if not loop.remove_signal_handler(sig):
                    signal.signal(sig, signal.SIG_DFL if sig == signal.SIGTERM else signal.default_int_handler)
            except (NotImplementedError, RuntimeError, ValueError):
                with contextlib.suppress(ValueError, OSError):
                    signal.signal(sig, signal.SIG_DFL if sig == signal.SIGTERM else signal.default_int_handler)
        self._signals_installed.clear()


__all__ = [
    "ALERTS_ONLY_BANNER",
    "EXIT_CONFIG",
    "EXIT_ERROR",
    "EXIT_OK",
    "MISSING_X_TOKEN",
    "NO_VERIFIED_ACCOUNTS",
    "ConfigProblem",
    "Service",
]
