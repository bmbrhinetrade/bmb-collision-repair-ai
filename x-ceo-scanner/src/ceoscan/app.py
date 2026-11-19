"""The ceoscan service: one process that ingests, scores, alerts and reports on itself.

``Service.run`` wires every module together:

* X ingest - the filtered stream (rules are synced first) with gap recovery through timeline
  polling after an outage, or timeline polling when the plan has no filtered stream. In
  ``x_api.mode: auto`` a stream that turns out to be unavailable falls back to polling
  automatically and the resulting latency is printed;
* the pipeline workers (fast rules -> Claude -> decision -> push);
* the health server (``GET /health``) and the ops scheduler (stream-down alerts, daily summary).

SIGINT / SIGTERM stop everything gracefully (a second signal forces it). Exit codes: 0 after a
requested shutdown, 1 when a component crashed, 2 for a configuration problem the user must fix
(systemd's ``RestartPreventExitStatus=2`` keeps it from restart-looping on those).

ALERTS ONLY: nothing in this package trades or connects to a brokerage.
"""

from __future__ import annotations

import asyncio
import contextlib
import functools
import logging
import signal
import sqlite3
from collections.abc import Awaitable, Callable, Coroutine
from typing import Any, TypeVar

import aiohttp

from .http import new_session
from . import __version__
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
from .xapi.stream import Backoff, FilteredStream, StreamUnavailable

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


class ConfigProblem(Exception):
    """A configuration problem only the user can fix; the service exits with code 2."""


def _describe_exc(exc: BaseException) -> str:
    """Exception type and message on one line (X errors never carry secrets)."""
    text = " ".join(str(exc).split())
    return f"{type(exc).__name__}: {text}"[:300] if text else type(exc).__name__


def describe_latency(latency: tuple[float, float]) -> str:
    return f"typical {latency[0]:.1f}s, worst {latency[1]:.1f}s"


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
        self.stop = asyncio.Event()
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
        self._exit_code = EXIT_OK
        self._tasks: set[asyncio.Task[Any]] = set()
        self._background: set[asyncio.Task[Any]] = set()
        self._signals_installed: list[signal.Signals] = []

    def __repr__(self) -> str:  # never show the config's secrets
        return f"Service(mode={self.mode!r}, state={self.status.state!r})"

    # ------------------------------------------------------------------ public API

    def request_stop(self) -> None:
        """Ask the service to shut down gracefully (what SIGINT/SIGTERM do)."""
        self.stop.set()

    async def run(self) -> int:
        """Run until stopped. Returns the process exit code (0 ok, 1 crash, 2 configuration)."""
        setup_logging(self.settings.ops.log_level, self.cfg.secrets.secret_values())
        self.out(ALERTS_ONLY_BANNER)
        logger.info("starting ceoscan %s (alerts only - never trades)", __version__)
        try:
            return await self._run()
        except ConfigProblem as exc:
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
            logger.info("scoring: Claude %s (effort %s, timeout %.1fs)", sc.model, sc.effort, sc.timeout_seconds)
            return ClaudeScorer(sc, key)
        if sc.enabled:
            logger.warning(
                "ANTHROPIC_API_KEY is not set - posts are scored by the fast rules only (noisier alerts)"
            )
        else:
            logger.warning("scoring.enabled is false - posts are scored by the fast rules only")
        return OfflineScorer(sc)

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

    # ------------------------------------------------------------------ main body

    async def _run(self) -> int:
        cfg, s = self.cfg, self.settings
        token = cfg.secrets.x_bearer_token
        if not token:
            raise ConfigProblem(MISSING_X_TOKEN)
        build = self._build_rules()
        verified = [a for a in cfg.watchlist.accounts if a.user_id]
        self._install_signal_handlers()

        async with contextlib.AsyncExitStack() as stack:
            try:
                store = Store(s.ops.db_path)
            except (OSError, sqlite3.Error) as exc:
                raise ConfigProblem(f"cannot open the database {s.ops.db_path!r} (ops.db_path): {exc}") from exc
            stack.callback(store.close)
            self.store = store
            session = await stack.enter_async_context(new_session())
            dispatcher = build_dispatcher(cfg, session)  # warns (names only) about missing channel secrets
            self.dispatcher = dispatcher
            scorer = self._make_scorer()
            stack.push_async_callback(scorer.aclose)
            queue: asyncio.Queue = asyncio.Queue(maxsize=max(0, int(s.ops.queue_max)))

            health = HealthServer(
                s.ops.health_host, s.ops.health_port, self.status, store, s, self.started_at, queue
            )
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

            client = XClient(session, token, s.x_api)
            report = await self._check_access(client, session, token, len(verified))
            if report is None or self.stop.is_set():
                logger.info("stop requested during startup")
                return self._exit_code
            self.access_report = report
            self.out(format_access_report(report))

            poller = TimelinePoller(client, cfg.watchlist, s.x_api, queue, self.status, store)
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

            self._spawn_background(self._startup_alert(report, poller, build, len(verified)), "startup-alert")

            pipeline = Pipeline(cfg, store, scorer, dispatcher, self.status)
            scheduler = OpsScheduler(cfg, store, self.status, dispatcher)
            return await self._supervise(
                {
                    "ingest": self._ingest(mode, client, session, token, build.rules, poller, queue),
                    "pipeline": pipeline.run(queue, self.stop, workers=s.ops.pipeline_workers),
                    "scheduler": scheduler.run(self.stop),
                }
            )

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
    ) -> None:
        xs = self.settings.x_api
        if mode == "stream":
            try:
                if not await self._sync_rules_until_done(client, rules):
                    return  # stop requested
                stream = FilteredStream(
                    session,
                    token,
                    xs,
                    queue,
                    self.status,
                    client=client,
                    on_reconnect_gap=self._gap_recovery(poller),
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
            logger.info("recovery: stream was down %.0fs; fetching missed posts from timelines", gap_seconds)
            n = await poller.recover()
            if self.store is not None:
                self.store.log_event("gap_recovery", f"gap {gap_seconds:.0f}s, {n} post(s) recovered")

        return recover

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
        self.status.notes.append(f"polling: every {poller.interval:.1f}s, est. latency {poller.latency[0]:.1f}s")

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

    async def _startup_alert(
        self, report: AccessReport, poller: TimelinePoller, build: RuleBuildResult, n_verified: int
    ) -> None:
        mode = report.mode
        latency = report.est_latency_s if mode == "stream" else poller.latency
        title = f"ceoscan started ({mode}, {n_verified} accounts, est. {latency[0]:.1f}s)"
        lines = [
            "Mode: filtered stream"
            if mode == "stream"
            else f"Mode: timeline polling (each account every {poller.interval:.1f}s)",
            f"Accounts: {n_verified} verified"
            + (f", {len(build.skipped_accounts)} skipped (no user_id)" if build.skipped_accounts else ""),
            f"Est. latency: {describe_latency(latency)}",
        ]
        if mode == "stream" and self.cfg.watchlist.keyword_rules:
            lines.append(f"Keyword rules: {len(self.cfg.watchlist.keyword_rules)}")
        if report.credits_ok is False:
            lines.append("X API credits are depleted - add credits at console.x.com; ceoscan keeps retrying.")
        lines.append("Alerts only - ceoscan never trades.")
        await self._send_system(title, "\n".join(lines), "startup")

    async def _fallback_alert(self, poller: TimelinePoller, why: str) -> None:
        body = (
            f"The filtered stream is not available for this X app ({why}).\n"
            f"Polling {len(poller.accounts)} account(s), each every {poller.interval:.1f}s; "
            f"est. latency {describe_latency(poller.latency)}."
        )
        await self._send_system("ceoscan switched to timeline polling", body, "poll_fallback")

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
            self._config_error(str(exc))
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
            logger.info("shutting down")
        finally:
            self.stop.set()
            await self._drain()
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
        logger.info("ceoscan stopped")

    # ------------------------------------------------------------------ signals

    def _on_signal(self, signame: str) -> None:
        if not self.stop.is_set():
            logger.info("received %s; shutting down gracefully (send it again to force)", signame)
            self.stop.set()
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
