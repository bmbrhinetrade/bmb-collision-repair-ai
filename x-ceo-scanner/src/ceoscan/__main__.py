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

Exit codes: 0 ok, 1 failure, 2 configuration problem (e.g. X_BEARER_TOKEN missing).
ALERTS ONLY: ceoscan never trades and never connects to a brokerage.
"""

from __future__ import annotations

import argparse
import asyncio
import sqlite3
import sys
from collections.abc import Callable, Coroutine
from pathlib import Path
from typing import Any

import aiohttp

from .http import new_session
from . import __version__
from .alerts.dispatcher import AlertDispatcher, build_dispatcher
from .alerts.format import format_system_alert
from .alerts.notifiers import Notifier
from .app import EXIT_CONFIG, EXIT_ERROR, EXIT_OK, MISSING_X_TOKEN, Service
from .config import AppConfig, RulesSettings, load_app_config, load_watchlist
from .logging_setup import setup_logging
from .ops.cost import estimate_monthly_cost, format_cost_estimate
from .replay import run_replay
from .store import Store
from .xapi.access import check_access, format_access_report
from .xapi.client import XApiError, XClient
from .xapi.rules import RuleBuildResult, RuleSyncPlan, build_rules, plan_sync, rule_errors, sync_rules
from .xapi.verify import format_verify_report, record_results, verify_accounts, write_user_ids

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
        if changed:
            print(f"Updated {changed} account(s) in {cfg.watchlist_path}.")
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
    return EXIT_ERROR if report.credits_ok is False else EXIT_OK


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
