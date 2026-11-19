"""Command line interface: ``python -m ceoscan [global flags] <command> [options]``.

Commands:

* ``run``                     the service (stream or poll, score, alert, health, scheduler)
* ``verify [--write] [--accept-warnings]``  look every handle up on X; ``--write`` stores user ids
* ``rules [--apply]``         show the stream rules, the sync plan and a dry-run validation;
                              ``--apply`` syncs them to X now
* ``check-access``            token, credits, usage, stream vs. poll access, expected latency
* ``cost``                    monthly X + Claude cost estimate (offline, no secrets needed)
* ``replay FILE [--claude] [--send] [--keep-times] [--db PATH]``  saved posts through the pipeline
                              without contacting X (offline, no secrets needed)
* ``test-alert [--message TEXT]``  send a system alert through every configured channel

Global flags (before the command): ``--settings``, ``--watchlist``, ``--env``.

Exit codes: 0 ok, 1 failure, 2 configuration problem (e.g. X_BEARER_TOKEN missing, a typo in a YAML
file). When ``run`` cannot even load its configuration it sends one "ceoscan stopped: configuration
problem" alert through whatever push channel has secrets, then exits 2, or - with
``ops.config_error_action: wait`` / env ``CEOSCAN_CONFIG_ERROR_ACTION=wait`` (the Docker image) -
stays up idle with /health answering 503 and state ``config_error``, so a container restart policy
cannot loop.
ALERTS ONLY: ceoscan never trades and never connects to a brokerage.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import logging
import signal
import sqlite3
import sys
from collections.abc import Callable, Coroutine
from pathlib import Path
from typing import Any

import aiohttp

from . import __version__
from .alerts.dispatcher import AlertDispatcher, build_dispatcher
from .alerts.format import format_system_alert
from .alerts.notifiers import Notifier
from .app import EXIT_CONFIG, EXIT_ERROR, EXIT_OK, MISSING_X_TOKEN, Service
from .config import (
    AppConfig,
    ConfigError,
    RulesSettings,
    fallback_settings,
    load_app_config,
    load_secrets,
    load_settings,
    load_watchlist,
)
from .http import new_session
from .logging_setup import RedactingFilter, setup_logging
from .models import IngestStatus, Watchlist
from .ops.cost import estimate_monthly_cost, format_cost_estimate
from .ops.health import HealthServer
from .ops.scheduler import send_config_error_alert
from .replay import run_replay
from .store import Store
from .timeutil import now_utc
from .xapi.access import AccessReport, check_access, format_access_report
from .xapi.client import XApiError, XClient
from .xapi.rules import (
    RuleBuildResult,
    RuleSyncPlan,
    build_rules,
    plan_sync,
    rule_errors,
    sync_rules,
)
from .xapi.verify import (
    batch_failure,
    format_verify_report,
    record_results,
    verify_accounts,
    write_user_ids,
)

DEFAULT_SETTINGS = "config/settings.yaml"
DEFAULT_WATCHLIST = "config/watchlist.yaml"
DEFAULT_ENV = ".env"

#: Log level for one-shot commands; their results are printed, logs only carry warnings.
CLI_LOG_LEVEL = "WARNING"

TEST_ALERT_TITLE = "ceoscan test alert"
TEST_ALERT_BODY = (
    "If you can read this, ceoscan alerts reach you on this channel. "
    "This is a test - not a market signal."
)

Handler = Callable[[AppConfig, argparse.Namespace], Coroutine[Any, Any, int]]

logger = logging.getLogger("ceoscan")


def _err(text: str) -> None:
    print(text, file=sys.stderr)


def _network_error(exc: BaseException) -> str:
    text = " ".join(str(exc).split())
    return f"{type(exc).__name__}: {text}"[:300] if text else type(exc).__name__


# --------------------------------------------------------------------------- parser


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m ceoscan",
        description=(
            "Real-time X (Twitter) CEO post scanner. ALERTS ONLY: it never places trades "
            "and never connects to a brokerage."
        ),
    )
    parser.add_argument("--version", action="version", version=f"ceoscan {__version__}")
    parser.add_argument(
        "--settings", default=DEFAULT_SETTINGS, metavar="PATH", help=f"settings file (default {DEFAULT_SETTINGS})"
    )
    parser.add_argument(
        "--watchlist", default=DEFAULT_WATCHLIST, metavar="PATH", help=f"watchlist file (default {DEFAULT_WATCHLIST})"
    )
    parser.add_argument(
        "--env", default=DEFAULT_ENV, metavar="PATH", help=f"dotenv file with secrets (default {DEFAULT_ENV})"
    )
    sub = parser.add_subparsers(dest="command", metavar="<command>")

    sub.add_parser("run", help="run the service: stream (or poll), score, alert, health server, scheduler")

    p = sub.add_parser("verify", help="look up every watchlist handle on X and report problems")
    p.add_argument("--write", action="store_true", help="store the verified user_ids in the watchlist file")
    p.add_argument(
        "--accept-warnings",
        action="store_true",
        help="with --write: also store user_ids of accounts that have warnings",
    )

    p = sub.add_parser("rules", help="show the filtered-stream rules, the sync plan and a dry-run validation")
    p.add_argument("--apply", action="store_true", help="sync the rules to X now")

    sub.add_parser("check-access", help="check token, credits, usage, stream vs. poll access and latency")
    sub.add_parser("cost", help="estimate the monthly X API + Claude cost from the watchlist (offline)")

    p = sub.add_parser("replay", help="run saved stream-shaped posts through the pipeline without X")
    p.add_argument("file", metavar="FILE", help="JSONL file of stream payloads (e.g. samples/sample_posts.jsonl)")
    p.add_argument("--claude", action="store_true", help="score with Claude (needs ANTHROPIC_API_KEY)")
    p.add_argument("--send", action="store_true", help="send alerts to the real configured channels")
    p.add_argument("--keep-times", action="store_true", help="keep the samples' original ids and times")
    p.add_argument("--db", metavar="PATH", default=None, help="SQLite file to keep the results (default: in memory)")

    p = sub.add_parser("test-alert", help="send a test alert through every configured channel")
    p.add_argument("--message", metavar="TEXT", default=None, help="text of the test alert")
    return parser


# --------------------------------------------------------------------------- helpers


def load_config(args: argparse.Namespace) -> AppConfig:
    if not Path(args.settings).exists():
        _err(f"warning: settings file {args.settings} not found - using the built-in defaults")
    return load_app_config(args.settings, args.watchlist, args.env)


def require_x_token(cfg: AppConfig, command: str) -> str | None:
    """The bearer token, or None after printing a clear error (callers then exit 2)."""
    token = cfg.secrets.x_bearer_token
    if not token:
        _err(f"error: `{command}` needs the X API. {MISSING_X_TOKEN}")
    return token


def _verified_count(cfg: AppConfig) -> int:
    return sum(1 for a in cfg.watchlist.accounts if a.user_id)


def format_rule_build(build: RuleBuildResult, rs: RulesSettings, source: Path) -> str:
    lines = [
        f"Filtered-stream rules built from {source}: {len(build.rules)} rule(s) "
        f"(limit {rs.max_rules} rules of at most {rs.max_rule_length} chars)"
    ]
    for r in build.rules:
        lines.append(f"  [{r.tag}] {len(r.value)} chars")
        lines.append(f"    {r.value}")
    if build.skipped_accounts:
        lines.append(
            f"Skipped - no verified user_id, NOT streamed ({len(build.skipped_accounts)}): "
            + ", ".join(f"@{h}" for h in build.skipped_accounts)
        )
        lines.append("  Run `python -m ceoscan verify --write` to verify them and store their user_ids.")
    other = [w for w in build.warnings if "have no verified user_id" not in w]
    if other:
        lines.append("Warnings:")
        lines.extend(f"  - {w}" for w in other)
    return "\n".join(lines)


def format_sync_plan(plan: RuleSyncPlan, manage_foreign: bool) -> str:
    foreign_action = "deleted" if manage_foreign else "kept"
    lines = [
        f"Sync plan against the rules on X: {len(plan.unchanged)} unchanged, {len(plan.to_add)} to add, "
        f"{len(plan.to_delete)} to delete, {len(plan.foreign)} foreign ({foreign_action})"
    ]
    for r in plan.unchanged:
        lines.append(f"  = [{r.get('tag') or 'no tag'}] id {r.get('id')}")
    for spec in plan.to_add:
        lines.append(f"  + [{spec.tag}] {spec.value}")
    for r in plan.to_delete:
        lines.append(f"  - [{r.get('tag') or 'no tag'}] id {r.get('id')}: {r.get('value')}")
    if not manage_foreign:
        for r in plan.foreign:
            lines.append(f"  ~ not created by ceoscan, kept: [{r.get('tag') or 'no tag'}] {r.get('value')}")
    return "\n".join(lines)


# --------------------------------------------------------------------------- commands


async def cmd_run(cfg: AppConfig, args: argparse.Namespace) -> int:
    return await Service(cfg).run()


async def cmd_verify(cfg: AppConfig, args: argparse.Namespace) -> int:
    token = require_x_token(cfg, "verify")
    if not token:
        return EXIT_CONFIG
    accounts = cfg.watchlist.accounts
    if not accounts:
        _err(f"error: the watchlist {cfg.watchlist_path} has no accounts")
        return EXIT_CONFIG
    if args.accept_warnings and not args.write:
        print("note: --accept-warnings only has an effect together with --write")
    price = cfg.settings.x_api.pricing.user_read
    print(
        f"Looking up {len(accounts)} handle(s) on X (GET /2/users/by; about ${len(accounts) * price:.2f} "
        "in user reads)..."
    )
    async with new_session() as session:
        client = XClient(session, token, cfg.settings.x_api)
        results = await verify_accounts(client, cfg.watchlist)

    failure = batch_failure(results)
    if failure:  # e.g. HTTP 402 for every handle: one clear line instead of a table of identical errors
        _err(f"error: {failure}")
        return EXIT_ERROR
    print(format_verify_report(results))

    try:
        store = Store(cfg.settings.ops.db_path)
        try:
            record_results(store, results)
        finally:
            store.close()
    except (OSError, sqlite3.Error) as exc:
        _err(f"warning: could not record the results in {cfg.settings.ops.db_path}: {exc}")

    if args.write:
        try:
            changed = write_user_ids(cfg.watchlist_path, results, accept_warnings=args.accept_warnings)
        except Exception as exc:  # file permissions, invalid result file ...: report, never half-write
            _err(f"error: could not update {cfg.watchlist_path}: {type(exc).__name__}: {exc}")
            return EXIT_ERROR
        id_changes = [r for r in results if r.id_changed and r.status == "warning" and args.accept_warnings]
        for r in id_changes:
            print(
                f"CHANGED user_id of @{r.handle}: {r.previous_user_id} -> {r.user_id} "
                "(written because of --accept-warnings; the old account is no longer watched)"
            )
        not_written = [r for r in results if r.id_changed and not args.accept_warnings]
        for r in not_written:
            print(f"NOT changed: @{r.handle} keeps user_id {r.previous_user_id} (X now returns {r.user_id}).")
        if changed:
            print(f"Updated {changed} account(s) in {cfg.watchlist_path}.")
        elif any(r.status == "error" for r in results):
            print(f"{cfg.watchlist_path} was not changed (some lookups failed - see above).")
        else:
            print(f"{cfg.watchlist_path} is already up to date.")
        try:
            wl = load_watchlist(cfg.watchlist_path)
            n_ok = sum(1 for a in wl.accounts if a.user_id)
            print(f"{n_ok} of {len(wl.accounts)} account(s) now have a user_id and will be watched.")
        except Exception as exc:  # pragma: no cover - write_user_ids validated the file already
            _err(f"warning: could not re-read {cfg.watchlist_path}: {exc}")
    elif any(r.user_id for r in results if r.status in ("ok", "warning")):
        print(f"Nothing was written. Run `verify --write` to store the user_ids in {cfg.watchlist_path}.")
    return EXIT_ERROR if any(r.status == "error" for r in results) else EXIT_OK


async def cmd_rules(cfg: AppConfig, args: argparse.Namespace) -> int:
    rs = cfg.settings.x_api.rules
    try:
        build = build_rules(cfg.watchlist, rs)
    except ValueError as exc:
        _err(f"error: cannot build the stream rules: {exc}")
        return EXIT_CONFIG
    print(format_rule_build(build, rs, cfg.watchlist_path))
    token = require_x_token(cfg, "rules --apply" if args.apply else "rules (sync plan and validation)")
    if not token:
        return EXIT_CONFIG
    if not build.rules:
        print("WARNING: no rules were built; syncing would remove every rule ceoscan created on X.")

    async with new_session() as session:
        client = XClient(session, token, cfg.settings.x_api)
        try:
            existing = await client.get_rules()
            plan = plan_sync(build.rules, existing, rs.tag_prefix, rs.manage_foreign_rules)
            print(format_sync_plan(plan, rs.manage_foreign_rules))
            if not args.apply:
                if not plan.to_add:
                    print("Dry-run validation: no new rules to validate.")
                else:
                    resp = await client.add_rules(plan.to_add, dry_run=True)
                    problems, duplicates = rule_errors(resp)
                    for note in duplicates:
                        print(f"  note: {note}")
                    if problems:
                        print("Dry-run validation FAILED - X rejected:")
                        for p in problems:
                            print(f"  - {p}")
                        return EXIT_ERROR
                    print(f"Dry-run validation: X accepted all {len(plan.to_add)} new rule(s).")
                if plan.to_add or plan.to_delete:
                    print("Nothing was changed on X. Run `rules --apply` (or just `run`) to sync.")
                else:
                    print("The rules on X are already in sync.")
                return EXIT_OK
            if not plan.to_add and not plan.to_delete:
                print("The rules on X are already in sync; nothing to apply.")
                return EXIT_OK
            applied = await sync_rules(client, build.rules, rs, apply=True)
        except ValueError as exc:
            print(f"X rejected the rules; nothing was changed: {exc}")
            return EXIT_ERROR
        except XApiError as exc:
            hint = ""
            if exc.status == 403:
                hint = " (this app/plan may have no filtered-stream access; `run` would poll timelines instead)"
            _err(f"error: X API request failed: {exc}{hint}")
            return EXIT_ERROR
        except (aiohttp.ClientError, OSError, TimeoutError) as exc:
            _err(f"error: could not reach the X API: {_network_error(exc)}")
            return EXIT_ERROR
    print(f"Applied: deleted {len(applied.to_delete)} rule(s), added {len(applied.to_add)} rule(s).")
    return EXIT_OK


async def cmd_check_access(cfg: AppConfig, args: argparse.Namespace) -> int:
    token = require_x_token(cfg, "check-access")
    if not token:
        return EXIT_CONFIG
    n = _verified_count(cfg)
    async with new_session() as session:
        client = XClient(session, token, cfg.settings.x_api)
        report = await check_access(client, cfg.settings.x_api, n, session, token)
    print(format_access_report(report))
    if n == 0:
        print("note: no account has a user_id yet - run `python -m ceoscan verify --write`.")
    return access_exit_code(report)


def access_exit_code(report: AccessReport) -> int:
    """2 when X rejected the bearer token, 1 when credits are depleted or no check got an answer, else 0."""
    auth_ok = getattr(report, "auth_ok", None)
    if auth_ok is None and any("rejected the bearer token" in m for m in report.messages):
        auth_ok = False  # older AccessReport without auth_ok: the 401 is only in the notes
    if auth_ok is False:
        _err(
            "error: X rejected the bearer token (HTTP 401). Copy the Bearer Token of your app at console.x.com "
            "into X_BEARER_TOKEN in .env again (no quotes or spaces), then recreate/restart."
        )
        return EXIT_CONFIG
    if report.credits_ok is False:
        _err("error: X API credits are depleted (HTTP 402) - add credits at console.x.com.")
        return EXIT_ERROR
    answered = (
        report.stream_available is not None
        or report.credits_ok is not None
        or report.usage is not None
        or report.existing_rules is not None
    )
    if not answered:
        _err("error: none of the access checks got an answer from X (network problem?); see the notes above.")
        return EXIT_ERROR
    return EXIT_OK


async def cmd_cost(cfg: AppConfig, args: argparse.Namespace) -> int:
    mode = "poll" if cfg.settings.x_api.mode == "poll" else "stream"
    print(format_cost_estimate(estimate_monthly_cost(cfg.watchlist, cfg.settings, mode=mode)))
    return EXIT_OK


async def cmd_replay(cfg: AppConfig, args: argparse.Namespace) -> int:
    path = Path(args.file)
    if not path.is_file():
        _err(f"error: replay file {path} not found")
        return EXIT_CONFIG
    try:
        await run_replay(
            cfg,
            path,
            use_claude=args.claude,
            send=args.send,
            db_path=args.db or ":memory:",
            rebase_times=not args.keep_times,
        )
    except ValueError as exc:  # malformed sample file
        _err(f"error: {exc}")
        return EXIT_CONFIG
    return EXIT_OK


async def cmd_test_alert(cfg: AppConfig, args: argparse.Namespace) -> int:
    msg = format_system_alert(TEST_ALERT_TITLE, args.message or TEST_ALERT_BODY, priority="normal")
    async with new_session() as session:
        configured = build_dispatcher(cfg, session)
        channels: list[Notifier] = [*configured.push]
        channels += [n for n in (configured.sms, configured.email) if n is not None]
        if not channels:
            _err(
                "error: no alert channel is configured. Set alerts.push in the settings file and the "
                "matching secrets in .env (e.g. NTFY_TOPIC for ntfy, PUSHOVER_APP_TOKEN + PUSHOVER_USER_KEY "
                "for Pushover); see .env.example."
            )
            return EXIT_CONFIG
        # Every configured channel gets the test, including SMS/email (which normally need a score).
        dispatcher = AlertDispatcher(cfg.settings.alerts, push=channels)
        print(f"Sending a test alert to: {', '.join(dispatcher.channel_names)}")
        results = await dispatcher.send(msg)
    width = max(len(r.channel) for r in results) if results else 0
    for r in results:
        state = "OK" if r.ok else "FAILED"
        detail = f"  {r.error}" if r.error else ""
        status = f" HTTP {r.status}" if r.status is not None and not r.ok and "HTTP" not in (r.error or "") else ""
        print(f"  {r.channel:<{width}}  {state:<6}  {r.elapsed_ms:>5} ms{status}{detail}")
    ok = bool(results) and all(r.ok for r in results)
    print("All channels delivered the test alert." if ok else "Some channels failed - see above.")
    return EXIT_OK if ok else EXIT_ERROR


def _config_problem_text(exc: BaseException) -> str:
    if isinstance(exc, ConfigError):
        return str(exc)
    return f"could not load the configuration: {type(exc).__name__}: {exc}"


async def run_config_error(
    args: argparse.Namespace,
    message: str,
    stop: asyncio.Event | None = None,
    install_signal_handlers: bool = True,
) -> int:
    """``run`` could not load its configuration: alert once (deduped), then exit 2 or stay up idle.

    ``ops.config_error_action`` comes from the settings file if that one loads, else from the
    environment (``CEOSCAN_CONFIG_ERROR_ACTION``, set to ``wait`` by the Docker image). With "wait"
    the health server answers 503 with state ``config_error`` until SIGTERM/SIGINT (or ``stop``).
    """
    try:
        settings = load_settings(args.settings)
        settings_ok = True
    except Exception:  # the settings file is (part of) the problem: built-in defaults + env overrides
        settings = fallback_settings()
        settings.alerts.push = "both"  # the user's choice is unknown: every push channel whose secrets are set
        settings_ok = False
    secrets = load_secrets()  # .env was read before the failing step
    setup_logging(settings.ops.log_level if settings_ok else "INFO", secrets.secret_values())
    message = RedactingFilter(secrets.secret_values()).redact(message)
    logger.error("configuration problem: %s", message)
    action = settings.ops.config_error_action
    cfg = AppConfig(settings, Watchlist((), (), (), {}), secrets, Path(args.settings), Path(args.watchlist))
    store: Store | None = None
    try:
        store = Store(settings.ops.db_path)
    except (OSError, sqlite3.Error) as exc:
        logger.warning("could not open %s (%s); the alert is sent without dedupe", settings.ops.db_path, exc)
    try:
        try:
            async with new_session() as session:
                await send_config_error_alert(store, build_dispatcher(cfg, session), message, action)
        except Exception:  # best effort: exiting / idling below must still happen
            logger.exception("could not send the configuration-problem alert")
        if action != "wait":
            return EXIT_CONFIG
        return await _idle_on_config_error(settings, store, message, stop, install_signal_handlers)
    finally:
        if store is not None:
            store.close()


async def _idle_on_config_error(
    settings: Any,
    store: Store | None,
    message: str,
    stop: asyncio.Event | None,
    install_signal_handlers: bool,
) -> int:
    stop = stop or asyncio.Event()
    loop = asyncio.get_running_loop()
    installed: list[signal.Signals] = []
    if install_signal_handlers:
        for sig in (signal.SIGINT, signal.SIGTERM):
            with contextlib.suppress(NotImplementedError, RuntimeError, ValueError):
                loop.add_signal_handler(sig, stop.set)
                installed.append(sig)
    status = IngestStatus(mode="idle", state="config_error", last_error=message[:300], down_since=now_utc())
    health_store = store or Store(":memory:")
    health = HealthServer(settings.ops.health_host, settings.ops.health_port, status, health_store, settings, now_utc())
    try:
        try:
            await health.start()
        except OSError as exc:
            logger.error("health server could not listen on %s:%s (%s)", health.host, health.port, exc)
        note = (
            "ceoscan is idle because of this configuration problem (ops.config_error_action: wait); "
            "/health reports state config_error. Fix the configuration and restart ceoscan."
        )
        print(note)
        logger.error("%s", note)
        await stop.wait()
    finally:
        await health.stop()
        if health_store is not store:
            health_store.close()
        for sig in installed:
            with contextlib.suppress(NotImplementedError, RuntimeError, ValueError):
                loop.remove_signal_handler(sig)
    return EXIT_CONFIG


COMMANDS: dict[str, Handler] = {
    "run": cmd_run,
    "verify": cmd_verify,
    "rules": cmd_rules,
    "check-access": cmd_check_access,
    "cost": cmd_cost,
    "replay": cmd_replay,
    "test-alert": cmd_test_alert,
}


# --------------------------------------------------------------------------- entry point


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if not args.command:
        parser.print_help(sys.stderr)
        return EXIT_CONFIG
    try:
        cfg = load_config(args)
    except Exception as exc:  # YAML/validation/file errors: config problems the user must fix
        _err(f"error: could not load the configuration: {type(exc).__name__}: {exc}")
        if args.command == "run":  # the service: alert once, then exit 2 or stay idle (Docker)
            try:
                return asyncio.run(run_config_error(args, _config_problem_text(exc)))
            except KeyboardInterrupt:
                return EXIT_CONFIG
        return EXIT_CONFIG
    if args.command != "run":  # `run` configures logging itself from ops.log_level
        setup_logging(CLI_LOG_LEVEL, cfg.secrets.secret_values())
    try:
        return asyncio.run(COMMANDS[args.command](cfg, args))
    except KeyboardInterrupt:
        _err("interrupted")
        return 130


if __name__ == "__main__":
    sys.exit(main())
