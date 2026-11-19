"""Configuration loading.

* Non-secret settings: config/settings.yaml   (thresholds, model, channels, quiet hours ...)
* Accounts / tickers:  config/watchlist.yaml  (easy to hand-edit)
* Secrets:             environment variables or a git-ignored .env file - never YAML.

Secrets are exposed only through `Secrets`, whose repr() masks values, so an
accidental log line of the config never leaks a key.

Both YAML files are validated strictly at load time (unknown keys, wrong types, bad time zones or
HH:MM times, out-of-range numbers), so a typo fails at startup with a clear ``ConfigError`` naming
the setting instead of silently breaking alerts later.
"""

from __future__ import annotations

import datetime as _dt
import difflib
import os
import re
import sys
import types
import typing
from dataclasses import MISSING, dataclass, field, fields, is_dataclass, replace as _dc_replace
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from ruamel.yaml import YAML

from .models import Account, KeywordRule, Watchlist

TICKER_RE = re.compile(r"^[A-Z][A-Z0-9.]{0,9}$")
HANDLE_RE = re.compile(r"^[A-Za-z0-9_]{1,15}$")
HHMM_RE = re.compile(r"^([01]?\d|2[0-3]):([0-5]\d)$")
LOG_LEVELS = ("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL")


class ConfigError(ValueError):
    pass


# --------------------------------------------------------------------------- settings


@dataclass
class StreamSettings:
    heartbeat_timeout_seconds: float = 30.0  # user requirement: reconnect if keep-alive stops for 30 s
    backfill_minutes: int = 0  # X allows 0-5; may require a higher tier, so off by default
    terminate_stale_connections: bool = True  # DELETE /2/connections/filtered_stream on TooManyConnections
    connect_timeout_seconds: float = 15.0
    credits_depleted_retry_seconds: float = 300.0


@dataclass
class PollSettings:
    # GET /2/users/:id/tweets app limit per docs.x.com/x-api/fundamentals/rate-limits (checked 2026-11-19)
    max_requests_per_15min: int = 10_000
    utilization: float = 0.8  # use 80% of the limit, leave headroom
    min_interval_seconds: float = 2.0  # never poll a single account faster than this
    concurrency: int = 4
    max_results: int = 10  # 5..100


@dataclass
class RecoverySettings:
    enabled: bool = True  # after a stream gap, poll each account's timeline with since_id
    # 0 = catch up after every reconnect: a pass is ~1 mostly empty (unbilled) request per account, and
    # posts in flight on a dropped connection are otherwise lost. The gap includes a 5 s delivery margin.
    min_gap_seconds: float = 0.0
    max_results: int = 20
    max_pages: int = 5  # follow pagination up to this many pages per account during catch-up
    max_age_seconds: float = 3600.0  # never catch up on posts older than this (cost + noise bound)
    on_startup: bool = True  # also catch up once after the first connect of a process (restarts/deploys)


@dataclass
class RulesSettings:
    max_rules: int = 1000  # pay-per-use limit
    max_rule_length: int = 1024  # pay-per-use limit
    tag_prefix: str = "ceoscan"
    manage_foreign_rules: bool = False  # if true, delete rules this app did not create


@dataclass
class XPricing:
    # docs.x.com/x-api/getting-started/pricing (checked 2026-11-19). Update if X changes prices.
    post_read: float = 0.005
    user_read: float = 0.010
    monthly_post_cap: int = 3_000_000
    checked: str = "2026-11-19"


@dataclass
class XApiSettings:
    mode: str = "auto"  # auto | stream | poll
    base_url: str = "https://api.x.com"
    stream: StreamSettings = field(default_factory=StreamSettings)
    poll: PollSettings = field(default_factory=PollSettings)
    recovery: RecoverySettings = field(default_factory=RecoverySettings)
    rules: RulesSettings = field(default_factory=RulesSettings)
    pricing: XPricing = field(default_factory=XPricing)
    # Fields requested from X. Kept minimal to control cost and payload size.
    tweet_fields: list[str] = field(
        default_factory=lambda: [
            "created_at",
            "author_id",
            "referenced_tweets",
            "entities",
            "public_metrics",
            "note_tweet",
            "conversation_id",
            "in_reply_to_user_id",
            "edit_history_tweet_ids",
            "lang",
        ]
    )
    expansions: list[str] = field(
        default_factory=lambda: ["author_id", "referenced_tweets.id", "referenced_tweets.id.author_id"]
    )
    user_fields: list[str] = field(default_factory=lambda: ["username", "name", "verified", "public_metrics"])


@dataclass
class ClaudePricing:
    input_per_mtok: float = 0.10  # claude-haiku-5-5, prompts <= 100K tokens
    output_per_mtok: float = 0.50


@dataclass
class ScoringSettings:
    model: str = "claude-haiku-5-5"
    effort: str = "low"  # low | medium | high
    thinking: str = "adaptive"  # adaptive | disabled  (disabled only valid at effort <= high)
    threshold: int = 7
    quiet_hours_threshold: int = 9
    joke_threshold: int = 9  # posts Claude marks as joke/meme alert only at this score or higher
    require_my_ticker: bool = True  # non-always-alert posts must affect at least one of my tickers
    score_always_alert_unflagged: bool = True  # Claude-score always-alert posts even if fast rules miss
    # Send every post from a watched account to Claude (fast rules become hints). Posts like
    # "Funding secured." or a CEO resignation slip past keyword rules; scoring them all costs < $1/month.
    # Keyword-rule (non-watchlist) posts are always gated by the fast rules.
    score_watchlist_unflagged: bool = True
    timeout_seconds: float = 4.0  # per-request timeout
    total_budget_seconds: float = 4.0  # hard cap on all Claude attempts for one post (latency target < 10 s)
    max_retries: int = 1  # only for fast failures (429/5xx/connection) when budget remains; never after a timeout
    fallback_max_impact: int = 7  # rules-only scores (Claude down / no key) never exceed this
    max_tokens: int = 1024
    enabled: bool = True  # false -> use rules_fallback scores only
    pricing: ClaudePricing = field(default_factory=ClaudePricing)


@dataclass
class QuietHours:
    enabled: bool = True
    start: str = "22:00"
    end: str = "06:30"
    always_alert_bypass: bool = False  # if true, always-alert accounts ignore quiet hours


@dataclass
class NtfySettings:
    server: str = "https://ntfy.sh"


@dataclass
class PushoverSettings:
    sound: str | None = None
    high_priority_min_score: int = 9  # Pushover priority 1 at/above this score


@dataclass
class SmsSettings:
    enabled: bool = False
    min_score: int = 8


@dataclass
class EmailSettings:
    enabled: bool = False
    min_score: int = 7
    send_daily_summary: bool = True
    use_ssl: bool = False  # true -> SMTPS (465); false -> STARTTLS
    # A full SMTP exchange (connect, STARTTLS, AUTH, DATA) can take several seconds; email has its own,
    # longer budget so a slow but successful send is not reported as failed. Push latency is unaffected.
    timeout_seconds: float = 20.0


@dataclass
class AlertSettings:
    timezone: str = "America/Los_Angeles"
    snippet_chars: int = 200
    push: str = "ntfy"  # ntfy | pushover | both | none
    quiet_hours: QuietHours = field(default_factory=QuietHours)
    ntfy: NtfySettings = field(default_factory=NtfySettings)
    pushover: PushoverSettings = field(default_factory=PushoverSettings)
    sms: SmsSettings = field(default_factory=SmsSettings)
    email: EmailSettings = field(default_factory=EmailSettings)
    send_timeout_seconds: float = 5.0
    # Posts older than this when received (gap recovery, catch-up) are stored and scored but pushed only
    # as low-priority "missed during outage" alerts (no SMS, no urgent) and left out of latency stats.
    max_post_age_seconds: float = 900.0


@dataclass
class DailySummarySettings:
    enabled: bool = True
    time: str = "18:00"  # local time in alerts.timezone


@dataclass
class OpsSettings:
    health_host: str = "127.0.0.1"  # env CEOSCAN_HEALTH_HOST overrides (the Docker image sets 0.0.0.0)
    health_port: int = 8080
    db_path: str = "data/ceoscan.db"
    log_level: str = "INFO"
    stream_down_alert_seconds: float = 120.0
    daily_summary: DailySummarySettings = field(default_factory=DailySummarySettings)
    pipeline_workers: int = 8
    queue_max: int = 10_000
    # What `run` does on a non-transient configuration error: "exit" (code 2; systemd will not restart) or
    # "wait" (stay up, report it on /health, never exit - avoids Docker restart loops). Env
    # CEOSCAN_CONFIG_ERROR_ACTION overrides; the Docker image sets "wait".
    config_error_action: str = "exit"


@dataclass
class Settings:
    x_api: XApiSettings = field(default_factory=XApiSettings)
    scoring: ScoringSettings = field(default_factory=ScoringSettings)
    alerts: AlertSettings = field(default_factory=AlertSettings)
    ops: OpsSettings = field(default_factory=OpsSettings)


def _did_you_mean(key: Any, allowed: typing.Iterable[str]) -> str:
    match = difflib.get_close_matches(str(key).replace("-", "_"), list(allowed), n=1, cutoff=0.6)
    return f" (did you mean {match[0]}?)" if match else ""


def _type_name(value: Any) -> str:
    return "nothing" if value is None else type(value).__name__


def _coerce(value: Any, hint: Any, path: str) -> Any:
    """Check a YAML scalar against a dataclass field's type; ints are accepted for floats."""
    origin = typing.get_origin(hint)
    if origin in (typing.Union, types.UnionType):
        args = typing.get_args(hint)
        if value is None and type(None) in args:
            return None
        non_none = [a for a in args if a is not type(None)]
        if len(non_none) == 1:
            return _coerce(value, non_none[0], path)
        return value
    if origin is list:
        (item_hint,) = typing.get_args(hint) or (Any,)
        if not isinstance(value, list):
            raise ConfigError(f"{path} must be a list, e.g. [a, b] (got {value!r})")
        return [_coerce(v, item_hint, f"{path}[{i}]") for i, v in enumerate(value)]
    if hint is bool:
        if not isinstance(value, bool):
            raise ConfigError(f"{path} must be true or false (got {value!r})")
        return value
    if hint is int:
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ConfigError(f"{path} must be a whole number (got {_type_name(value)} {value!r})")
        if isinstance(value, float):
            if not value.is_integer():
                raise ConfigError(f"{path} must be a whole number (got {value!r})")
            return int(value)
        return value
    if hint is float:
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ConfigError(f"{path} must be a number (got {_type_name(value)} {value!r})")
        return float(value)
    if hint is str:
        if isinstance(value, _dt.date):  # an unquoted date such as checked: 2026-11-19
            return value.isoformat()
        if not isinstance(value, str):
            raise ConfigError(f"{path} must be a text value; put it in quotes (got {_type_name(value)} {value!r})")
        return value
    return value


def _build(cls: type, data: dict[str, Any] | None, path: str) -> Any:
    """Build a (nested) dataclass from a dict, rejecting unknown keys and wrong types with a clear error."""
    if data is None:
        data = {}
    if not isinstance(data, dict):
        raise ConfigError(f"{path} must be a mapping of settings (got {_type_name(data)} {data!r})")
    data = dict(data)
    kwargs: dict[str, Any] = {}
    known = {f.name: f for f in fields(cls)}
    hints = typing.get_type_hints(cls)
    unknown = [k for k in data if k not in known]
    if unknown:
        details = ", ".join(f"{k}{_did_you_mean(k, known)}" for k in sorted(map(str, unknown)))
        raise ConfigError(f"unknown setting(s) at {path}: {details}")
    for name, f in known.items():
        if name not in data:
            continue
        value = data[name]
        default_obj = f.default_factory() if f.default_factory is not MISSING else None
        if default_obj is not None and is_dataclass(default_obj):
            kwargs[name] = _build(type(default_obj), value, f"{path}.{name}")
        else:
            kwargs[name] = _coerce(value, hints.get(name, Any), f"{path}.{name}")
    return cls(**kwargs)


# Non-secret deployment overrides read from the environment (used by the Docker image).
ENV_OVERRIDES = {
    "CEOSCAN_HEALTH_HOST": ("ops", "health_host"),
    "CEOSCAN_CONFIG_ERROR_ACTION": ("ops", "config_error_action"),
}


def _apply_env_overrides(settings: Settings) -> None:
    for env_name, (section, attr) in ENV_OVERRIDES.items():
        value = os.environ.get(env_name)
        if value and value.strip():
            setattr(getattr(settings, section), attr, value.strip())


def fallback_settings() -> Settings:
    """Built-in defaults plus the environment overrides (used when settings.yaml itself is broken)."""
    settings = Settings()
    _apply_env_overrides(settings)
    if settings.ops.config_error_action not in ("exit", "wait"):
        settings.ops.config_error_action = "exit"
    return settings


def load_settings(path: str | Path) -> Settings:
    p = Path(path)
    if p.exists():
        raw = YAML(typ="safe").load(p.read_text(encoding="utf-8")) or {}
        settings = _build(Settings, raw, "settings")
    else:
        settings = Settings()
    _apply_env_overrides(settings)
    _validate_settings(settings)
    return settings


# Models on which thinking cannot be turned off: {"type": "disabled"} is a 400 at every effort level
# (Claude Opus 5.5, Claude Sonnet 5.5, every Fable / Mythos model). Others (e.g. claude-haiku-5-5,
# claude-opus-5) accept it at effort low/medium/high only, which the effort check covers.
_THINKING_ALWAYS_ON_PREFIXES = ("claude-opus-5-5", "claude-sonnet-5-5", "claude-fable-", "claude-mythos-")


def _thinking_always_on(model: str | None) -> bool:
    name = (model or "").strip().lower()
    return any(name.startswith(prefix) for prefix in _THINKING_ALWAYS_ON_PREFIXES)


def _check_hhmm(value: Any, path: str, problems: list[str]) -> None:
    if not isinstance(value, str) or not HHMM_RE.match(value.strip()):
        problems.append(f'{path} must be a 24-hour time "HH:MM" in quotes, e.g. "22:00" (got {value!r})')


def _check_timezone(value: Any, path: str, problems: list[str]) -> None:
    if not isinstance(value, str) or not value.strip():
        problems.append(f"{path} must be an IANA time zone name such as America/Los_Angeles (got {value!r})")
        return
    try:
        ZoneInfo(value.strip())
    except (ZoneInfoNotFoundError, ValueError, OSError):
        problems.append(f"{path}: unknown time zone {value!r} (use an IANA name such as America/Los_Angeles)")


def _settings_problems(s: Settings) -> list[str]:
    """Every problem in the (already type-checked) settings, as human-readable lines."""
    p: list[str] = []

    def one_of(value: Any, allowed: tuple[str, ...], path: str, env: str | None = None) -> None:
        if value not in allowed:
            src = f" (from the environment variable {env})" if env and os.environ.get(env, "").strip() else ""
            p.append(f"{path} must be {', '.join(allowed[:-1])} or {allowed[-1]} (got {value!r}){src}")

    def at_least(value: float, low: float, path: str) -> None:
        if value < low:
            p.append(f"{path} must be >= {low:g} (got {value!r})")

    def positive(value: float, path: str) -> None:
        if not value > 0:
            p.append(f"{path} must be > 0 (got {value!r})")

    def between(value: float, low: float, high: float, path: str) -> None:
        if not low <= value <= high:
            p.append(f"{path} must be {low:g}-{high:g} (got {value!r})")

    x, sc, al, ops = s.x_api, s.scoring, s.alerts, s.ops
    one_of(x.mode, ("auto", "stream", "poll"), "x_api.mode")
    if not re.match(r"^https?://\S+$", x.base_url or ""):
        p.append(f"x_api.base_url must start with https:// (got {x.base_url!r})")
    positive(x.stream.heartbeat_timeout_seconds, "x_api.stream.heartbeat_timeout_seconds")
    between(x.stream.backfill_minutes, 0, 5, "x_api.stream.backfill_minutes")
    positive(x.stream.connect_timeout_seconds, "x_api.stream.connect_timeout_seconds")
    positive(x.stream.credits_depleted_retry_seconds, "x_api.stream.credits_depleted_retry_seconds")
    at_least(x.poll.max_requests_per_15min, 1, "x_api.poll.max_requests_per_15min")
    if not 0 < x.poll.utilization <= 1:
        p.append(f"x_api.poll.utilization must be > 0 and <= 1 (got {x.poll.utilization!r})")
    positive(x.poll.min_interval_seconds, "x_api.poll.min_interval_seconds")
    at_least(x.poll.concurrency, 1, "x_api.poll.concurrency")
    between(x.poll.max_results, 5, 100, "x_api.poll.max_results")
    at_least(x.recovery.min_gap_seconds, 0, "x_api.recovery.min_gap_seconds")
    between(x.recovery.max_results, 5, 100, "x_api.recovery.max_results")
    at_least(x.recovery.max_pages, 1, "x_api.recovery.max_pages")
    positive(x.recovery.max_age_seconds, "x_api.recovery.max_age_seconds")
    at_least(x.rules.max_rules, 1, "x_api.rules.max_rules")
    at_least(x.rules.max_rule_length, 64, "x_api.rules.max_rule_length")
    if not re.match(r"^[A-Za-z0-9_-]{1,32}$", x.rules.tag_prefix or ""):
        p.append(f"x_api.rules.tag_prefix must be 1-32 letters, digits, '_' or '-' (got {x.rules.tag_prefix!r})")
    for name in ("post_read", "user_read"):
        at_least(getattr(x.pricing, name), 0, f"x_api.pricing.{name}")
    at_least(x.pricing.monthly_post_cap, 0, "x_api.pricing.monthly_post_cap")
    for name, items in (("tweet_fields", x.tweet_fields), ("expansions", x.expansions), ("user_fields", x.user_fields)):
        if any(not item.strip() or "," in item for item in items):
            p.append(f"x_api.{name} must be a list of single field names (no empty entries or commas)")

    if not (sc.model or "").strip():
        p.append("scoring.model must not be empty")
    one_of(sc.effort, ("low", "medium", "high", "xhigh", "max"), "scoring.effort")
    one_of(sc.thinking, ("adaptive", "disabled"), "scoring.thinking")
    if sc.thinking == "disabled" and _thinking_always_on(sc.model):
        p.append(
            f"scoring.thinking=disabled is rejected by {sc.model.strip()} at every effort (HTTP 400); "
            "use thinking: adaptive with effort: low"
        )
    elif sc.thinking == "disabled" and sc.effort in ("xhigh", "max"):
        p.append("scoring.thinking=disabled requires effort low/medium/high")
    for name in ("threshold", "quiet_hours_threshold", "joke_threshold", "fallback_max_impact"):
        between(getattr(sc, name), 1, 10, f"scoring.{name}")
    positive(sc.timeout_seconds, "scoring.timeout_seconds")
    positive(sc.total_budget_seconds, "scoring.total_budget_seconds")
    at_least(sc.max_retries, 0, "scoring.max_retries")
    at_least(sc.max_tokens, 16, "scoring.max_tokens")
    at_least(sc.pricing.input_per_mtok, 0, "scoring.pricing.input_per_mtok")
    at_least(sc.pricing.output_per_mtok, 0, "scoring.pricing.output_per_mtok")

    _check_timezone(al.timezone, "alerts.timezone", p)
    at_least(al.snippet_chars, 20, "alerts.snippet_chars")
    one_of(al.push, ("ntfy", "pushover", "both", "none"), "alerts.push")
    _check_hhmm(al.quiet_hours.start, "alerts.quiet_hours.start", p)
    _check_hhmm(al.quiet_hours.end, "alerts.quiet_hours.end", p)
    if not re.match(r"^https?://\S+$", al.ntfy.server or ""):
        p.append(f"alerts.ntfy.server must start with https:// (got {al.ntfy.server!r})")
    between(al.pushover.high_priority_min_score, 1, 11, "alerts.pushover.high_priority_min_score")
    between(al.sms.min_score, 1, 11, "alerts.sms.min_score")
    between(al.email.min_score, 1, 11, "alerts.email.min_score")
    positive(al.email.timeout_seconds, "alerts.email.timeout_seconds")
    positive(al.send_timeout_seconds, "alerts.send_timeout_seconds")
    positive(al.max_post_age_seconds, "alerts.max_post_age_seconds")

    if not (ops.health_host or "").strip():
        p.append("ops.health_host must not be empty (127.0.0.1 = this machine only, 0.0.0.0 = every interface)")
    between(ops.health_port, 0, 65535, "ops.health_port")
    if not (ops.db_path or "").strip():
        p.append("ops.db_path must not be empty")
    if str(ops.log_level).upper() not in LOG_LEVELS:
        p.append(f"ops.log_level must be one of {', '.join(LOG_LEVELS)} (got {ops.log_level!r})")
    positive(ops.stream_down_alert_seconds, "ops.stream_down_alert_seconds")
    _check_hhmm(ops.daily_summary.time, "ops.daily_summary.time", p)
    at_least(ops.pipeline_workers, 1, "ops.pipeline_workers")
    at_least(ops.queue_max, 0, "ops.queue_max")
    one_of(ops.config_error_action, ("exit", "wait"), "ops.config_error_action", env="CEOSCAN_CONFIG_ERROR_ACTION")
    return p


def _validate_settings(s: Settings) -> None:
    problems = _settings_problems(s)
    if len(problems) == 1:
        raise ConfigError(problems[0])
    if problems:
        raise ConfigError("invalid settings:\n  - " + "\n  - ".join(problems))


# --------------------------------------------------------------------------- watchlist


def _norm_ticker(t: Any, path: str = "ticker") -> str:
    if isinstance(t, bool) or not isinstance(t, (str, int)):
        raise ConfigError(f"{path}: invalid ticker {t!r}")
    t = str(t).strip().upper().lstrip("$")
    if not TICKER_RE.match(t):
        raise ConfigError(f"{path}: invalid ticker {t!r}")
    return t


WATCHLIST_KEYS = ("ticker_groups", "my_tickers", "accounts", "keyword_rules", "company_names")
ACCOUNT_KEYS = (
    "handle",
    "kind",
    "expect_name",
    "tickers",
    "always_alert",
    "include_replies",
    "include_retweets",
    "est_posts_per_day",
    "user_id",
    "notes",
)
KEYWORD_RULE_KEYS = ("name", "tickers", "terms", "min_followers", "extra_operators", "est_posts_per_day")
# A keyword-rule term: a $CASHTAG, a word or a plain phrase. No quotes, parentheses or ":" (X operators).
_KW_TERM_RE = re.compile(r"^\$?[A-Za-z0-9][A-Za-z0-9 ._'&+-]{0,59}$")


def _check_keys(item: Any, allowed: tuple[str, ...], path: str) -> dict[str, Any]:
    """``item`` must be a mapping whose keys are all in ``allowed``."""
    if not isinstance(item, dict):
        raise ConfigError(f"{path} must be a mapping (key: value lines), got {_type_name(item)} {item!r}")
    unknown = [k for k in item if k not in allowed]
    if unknown:
        details = ", ".join(f"{k}{_did_you_mean(k, allowed)}" for k in map(str, unknown))
        raise ConfigError(f"unknown key(s) at {path}: {details}. Allowed keys: {', '.join(allowed)}")
    return item


def _as_list(value: Any, path: str, example: str) -> list[Any]:
    """A YAML list (None -> []). A scalar is refused: iterating "TSLA" would give T, S, L, A."""
    if value is None:
        return []
    if isinstance(value, (list, tuple)):
        return list(value)
    raise ConfigError(f"{path} must be a list, e.g. {example} (got {_type_name(value)} {value!r})")


def _wl_bool(item: dict[str, Any], key: str, default: bool, path: str) -> bool:
    value = item.get(key, default)
    if not isinstance(value, bool):  # bool("false") would be True
        raise ConfigError(f"{path}.{key} must be true or false (got {value!r})")
    return value


def _wl_number(item: dict[str, Any], key: str, default: float, path: str) -> float:
    value = item.get(key, default)
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value < 0:
        raise ConfigError(f"{path}.{key} must be a number >= 0 (got {value!r})")
    return float(value)


def _wl_text(item: dict[str, Any], key: str, path: str) -> str | None:
    value = item.get(key)
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (str, int, float)):
        raise ConfigError(f"{path}.{key} must be text (got {_type_name(value)} {value!r})")
    return str(value)


def load_watchlist(path: str | Path) -> Watchlist:
    raw = YAML(typ="safe").load(Path(path).read_text(encoding="utf-8"))
    if raw is None:
        raw = {}
    _check_keys(raw, WATCHLIST_KEYS, "the watchlist (top level)")

    # Named ticker groups, e.g. big_tech: [AAPL, MSFT, ...]; referenced as "@big_tech".
    raw_groups = raw.get("ticker_groups") or {}
    if not isinstance(raw_groups, dict):
        raise ConfigError(f"ticker_groups must be a mapping, e.g. big_tech: [AAPL, MSFT] (got {raw_groups!r})")
    groups: dict[str, list[str]] = {
        str(k): [_norm_ticker(t, f"ticker_groups.{k}") for t in _as_list(v, f"ticker_groups.{k}", "[AAPL, MSFT]")]
        for k, v in raw_groups.items()
    }

    def expand(items: Any, where: str) -> tuple[str, ...]:
        out: list[str] = []
        for it in _as_list(items, where, "[TSLA] or [TSLA, \"@big_tech\"]"):
            s = str(it).strip() if isinstance(it, str) else it
            if isinstance(s, str) and s.startswith("@"):
                if s[1:] not in groups:
                    known = ", ".join(f"@{g}" for g in groups) or "none defined"
                    raise ConfigError(f"{where}: unknown ticker group {s!r} (ticker_groups: {known})")
                out.extend(groups[s[1:]])
            else:
                out.append(_norm_ticker(s, where))
        return tuple(dict.fromkeys(out))

    accounts: list[Account] = []
    seen: set[str] = set()
    for i, a in enumerate(_as_list(raw.get("accounts"), "accounts", "[{handle: elonmusk, tickers: [TSLA]}]")):
        where = f"accounts[{i}]"
        if isinstance(a, dict) and a.get("handle") is not None:
            where = f"accounts[{i}] (@{str(a.get('handle')).strip().lstrip('@')})"
        a = _check_keys(a, ACCOUNT_KEYS, where)
        handle = str(a.get("handle", "") or "").strip().lstrip("@")
        if not HANDLE_RE.match(handle):
            raise ConfigError(f"{where}: invalid or missing handle {handle!r}")
        if handle.lower() in seen:
            raise ConfigError(f"duplicate handle {handle!r} in watchlist")
        seen.add(handle.lower())
        user_id = a.get("user_id")
        if user_id is not None:
            user_id = str(user_id).strip()
            if not user_id.isdigit():
                raise ConfigError(f"{handle}: user_id must be numeric")
        kind = a.get("kind", "person")
        if kind not in ("person", "company"):
            raise ConfigError(f"{handle}: kind must be person or company")
        accounts.append(
            Account(
                handle=handle,
                tickers=expand(a.get("tickers"), f"{where}.tickers"),
                kind=kind,
                expect_name=_wl_text(a, "expect_name", where),
                user_id=user_id or None,
                always_alert=_wl_bool(a, "always_alert", False, where),
                include_replies=_wl_bool(a, "include_replies", True, where),
                include_retweets=_wl_bool(a, "include_retweets", False, where),
                est_posts_per_day=_wl_number(a, "est_posts_per_day", 3.0, where),
                notes=_wl_text(a, "notes", where) or "",
            )
        )
    resolved_accounts = accounts

    kw_rules: list[KeywordRule] = []
    for i, k in enumerate(_as_list(raw.get("keyword_rules"), "keyword_rules", "[{name: big-accounts, tickers: all}]")):
        where = f"keyword_rules[{i}]"
        k = _check_keys(k, KEYWORD_RULE_KEYS, where)
        tickers_cfg = k.get("tickers", "all")
        min_followers = k.get("min_followers", 1_000_000)
        if isinstance(min_followers, bool) or not isinstance(min_followers, int) or min_followers < 0:
            raise ConfigError(f"{where}.min_followers must be a whole number >= 0 (got {min_followers!r})")
        extra = k.get("extra_operators", "-is:retweet -is:reply")
        if extra is None:
            extra = ""
        if not isinstance(extra, str):
            raise ConfigError(f"{where}.extra_operators must be text (got {extra!r})")
        terms_cfg = k.get("terms") or []
        if not isinstance(terms_cfg, list):
            raise ConfigError(f"{where}.terms must be a list, e.g. [Apnimed, AD109, \"Talen Energy\"] (got {terms_cfg!r})")
        terms: list[str] = []
        for t in terms_cfg:
            text = " ".join(str(t).split()) if isinstance(t, (str, int)) and not isinstance(t, bool) else None
            if not text or not _KW_TERM_RE.match(text):
                raise ConfigError(
                    f"{where}.terms: {t!r} is not a valid term - use a $CASHTAG, a word or a plain phrase"
                    " (letters, digits, spaces, . _ ' & + -; at most 60 characters; no quotes, parentheses or ':')"
                )
            terms.append(text)
        tickers = ("__ALL__",) if tickers_cfg == "all" else expand(tickers_cfg, f"{where}.tickers")
        if not tickers and not terms:
            raise ConfigError(f"{where} needs tickers or terms")
        kw_rules.append(
            KeywordRule(
                name=str(k.get("name") or f"keywords-{i}"),
                tickers=tickers,
                min_followers=min_followers,
                extra_operators=extra,
                est_posts_per_day=_wl_number(k, "est_posts_per_day", 50.0, where),
                terms=tuple(dict.fromkeys(terms)),
            )
        )

    my = list(expand(raw.get("my_tickers"), "my_tickers"))
    for a in resolved_accounts:
        my.extend(a.tickers)
    for k in kw_rules:
        if k.tickers != ("__ALL__",):
            my.extend(k.tickers)
    my_tickers = tuple(dict.fromkeys(my))
    # "all" keyword rules mean "every ticker I care about".
    kw_rules = [
        _dc_replace(k, tickers=my_tickers)
        if k.tickers == ("__ALL__",)
        else k
        for k in kw_rules
    ]

    raw_names = raw.get("company_names") or {}
    if not isinstance(raw_names, dict):
        raise ConfigError(f"company_names must be a mapping, e.g. TSLA: [Tesla] (got {raw_names!r})")
    company_names: dict[str, tuple[str, ...]] = {}
    for t, names in raw_names.items():
        where = f"company_names.{t}"
        items = _as_list(names, where, "[Tesla, \"Model Y\"]")
        if any(not isinstance(n, (str, int)) or isinstance(n, bool) or not str(n).strip() for n in items):
            raise ConfigError(f"{where} must be a list of names (text)")
        company_names[_norm_ticker(t, where)] = tuple(str(n) for n in items)
    return Watchlist(
        accounts=tuple(resolved_accounts),
        keyword_rules=tuple(kw_rules),
        my_tickers=my_tickers,
        company_names=company_names,
    )


# --------------------------------------------------------------------------- secrets


def load_dotenv_if_present(path: str | Path = ".env") -> None:
    """Load KEY=VALUE pairs from .env without overriding real environment variables.

    An unreadable .env is not fatal: under systemd, ``EnvironmentFile=`` (read by PID 1 as root)
    has usually provided the variables already. A one-line warning naming only the path goes to
    stderr (logging is not set up yet); if a required secret really is missing, the normal
    "X_BEARER_TOKEN is not set" check reports it.
    """
    try:
        from dotenv import load_dotenv
    except ImportError:  # pragma: no cover - dependency is in requirements
        return
    p = Path(path)
    try:
        if not p.exists():
            return
        load_dotenv(p, override=False)
    except (OSError, UnicodeDecodeError) as exc:
        reason = exc.strerror if isinstance(exc, OSError) and exc.strerror else type(exc).__name__
        print(f"warning: cannot read {p} ({reason}); using the existing environment", file=sys.stderr)


SECRET_ENV_VARS = (
    "X_BEARER_TOKEN",
    "ANTHROPIC_API_KEY",
    "PUSHOVER_APP_TOKEN",
    "PUSHOVER_USER_KEY",
    "NTFY_TOPIC",
    "NTFY_TOKEN",
    "TWILIO_ACCOUNT_SID",
    "TWILIO_AUTH_TOKEN",
    "SMTP_PASSWORD",
)


@dataclass(frozen=True)
class Secrets:
    x_bearer_token: str | None = None
    anthropic_api_key: str | None = None
    pushover_app_token: str | None = None
    pushover_user_key: str | None = None
    ntfy_topic: str | None = None
    ntfy_token: str | None = None
    twilio_account_sid: str | None = None
    twilio_auth_token: str | None = None
    twilio_from: str | None = None
    sms_to: str | None = None
    smtp_host: str | None = None
    smtp_port: int = 587
    smtp_username: str | None = None
    smtp_password: str | None = None
    email_from: str | None = None
    email_to: str | None = None

    def __repr__(self) -> str:  # never print secret values
        present = [f.name for f in fields(self) if isinstance(getattr(self, f.name), str) and getattr(self, f.name)]
        return f"Secrets(present={present})"

    __str__ = __repr__

    def secret_values(self) -> list[str]:
        """All secret strings, for the log-redaction filter."""
        names = (
            "x_bearer_token",
            "anthropic_api_key",
            "pushover_app_token",
            "pushover_user_key",
            "ntfy_topic",
            "ntfy_token",
            "twilio_account_sid",
            "twilio_auth_token",
            "smtp_password",
        )
        return [v for n in names if (v := getattr(self, n)) and len(v) >= 6]


def load_secrets() -> Secrets:
    env = os.environ.get

    def clean(name: str) -> str | None:
        v = env(name)
        return v.strip() if v and v.strip() else None

    port = clean("SMTP_PORT")
    return Secrets(
        x_bearer_token=clean("X_BEARER_TOKEN"),
        anthropic_api_key=clean("ANTHROPIC_API_KEY"),
        pushover_app_token=clean("PUSHOVER_APP_TOKEN"),
        pushover_user_key=clean("PUSHOVER_USER_KEY"),
        ntfy_topic=clean("NTFY_TOPIC"),
        ntfy_token=clean("NTFY_TOKEN"),
        twilio_account_sid=clean("TWILIO_ACCOUNT_SID"),
        twilio_auth_token=clean("TWILIO_AUTH_TOKEN"),
        twilio_from=clean("TWILIO_FROM"),
        sms_to=clean("ALERT_SMS_TO"),
        smtp_host=clean("SMTP_HOST"),
        smtp_port=int(port) if port else 587,
        smtp_username=clean("SMTP_USERNAME"),
        smtp_password=clean("SMTP_PASSWORD"),
        email_from=clean("ALERT_EMAIL_FROM"),
        email_to=clean("ALERT_EMAIL_TO"),
    )


@dataclass
class AppConfig:
    settings: Settings
    watchlist: Watchlist
    secrets: Secrets
    settings_path: Path
    watchlist_path: Path


def load_app_config(
    settings_path: str | Path = "config/settings.yaml",
    watchlist_path: str | Path = "config/watchlist.yaml",
    dotenv_path: str | Path = ".env",
) -> AppConfig:
    load_dotenv_if_present(dotenv_path)
    return AppConfig(
        settings=load_settings(settings_path),
        watchlist=load_watchlist(watchlist_path),
        secrets=load_secrets(),
        settings_path=Path(settings_path),
        watchlist_path=Path(watchlist_path),
    )
