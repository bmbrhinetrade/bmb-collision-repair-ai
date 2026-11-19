"""Configuration loading.

* Non-secret settings: config/settings.yaml   (thresholds, model, channels, quiet hours ...)
* Accounts / tickers:  config/watchlist.yaml  (easy to hand-edit)
* Secrets:             environment variables or a git-ignored .env file - never YAML.

Secrets are exposed only through `Secrets`, whose repr() masks values, so an
accidental log line of the config never leaks a key.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field, fields, is_dataclass
from pathlib import Path
from typing import Any

from ruamel.yaml import YAML

from .models import Account, KeywordRule, Watchlist

TICKER_RE = re.compile(r"^[A-Z][A-Z0-9.]{0,9}$")
HANDLE_RE = re.compile(r"^[A-Za-z0-9_]{1,15}$")


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
    min_gap_seconds: float = 20.0
    max_results: int = 20


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
    timeout_seconds: float = 4.0
    max_retries: int = 1
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


@dataclass
class DailySummarySettings:
    enabled: bool = True
    time: str = "18:00"  # local time in alerts.timezone


@dataclass
class OpsSettings:
    health_host: str = "0.0.0.0"
    health_port: int = 8080
    db_path: str = "data/ceoscan.db"
    log_level: str = "INFO"
    stream_down_alert_seconds: float = 120.0
    daily_summary: DailySummarySettings = field(default_factory=DailySummarySettings)
    pipeline_workers: int = 4
    queue_max: int = 10_000


@dataclass
class Settings:
    x_api: XApiSettings = field(default_factory=XApiSettings)
    scoring: ScoringSettings = field(default_factory=ScoringSettings)
    alerts: AlertSettings = field(default_factory=AlertSettings)
    ops: OpsSettings = field(default_factory=OpsSettings)


def _build(cls: type, data: dict[str, Any] | None, path: str) -> Any:
    """Build a (nested) dataclass from a dict, rejecting unknown keys with a clear error."""
    data = dict(data or {})
    kwargs: dict[str, Any] = {}
    known = {f.name: f for f in fields(cls)}
    unknown = set(data) - set(known)
    if unknown:
        raise ConfigError(f"unknown setting(s) at {path}: {', '.join(sorted(unknown))}")
    for name, f in known.items():
        if name not in data:
            continue
        value = data[name]
        default_obj = f.default_factory() if callable(f.default_factory) else None  # type: ignore[misc]
        if default_obj is not None and is_dataclass(default_obj):
            kwargs[name] = _build(type(default_obj), value, f"{path}.{name}")
        else:
            kwargs[name] = value
    return cls(**kwargs)


def load_settings(path: str | Path) -> Settings:
    p = Path(path)
    if not p.exists():
        return Settings()
    raw = YAML(typ="safe").load(p.read_text(encoding="utf-8")) or {}
    settings = _build(Settings, raw, "settings")
    _validate_settings(settings)
    return settings


def _validate_settings(s: Settings) -> None:
    if s.x_api.mode not in ("auto", "stream", "poll"):
        raise ConfigError("x_api.mode must be auto, stream or poll")
    if s.alerts.push not in ("ntfy", "pushover", "both", "none"):
        raise ConfigError("alerts.push must be ntfy, pushover, both or none")
    if s.scoring.effort not in ("low", "medium", "high", "xhigh", "max"):
        raise ConfigError("scoring.effort must be low, medium, high, xhigh or max")
    if s.scoring.thinking not in ("adaptive", "disabled"):
        raise ConfigError("scoring.thinking must be adaptive or disabled")
    if s.scoring.thinking == "disabled" and s.scoring.effort in ("xhigh", "max"):
        raise ConfigError("scoring.thinking=disabled requires effort low/medium/high")
    for name in ("threshold", "quiet_hours_threshold", "joke_threshold"):
        v = getattr(s.scoring, name)
        if not 1 <= int(v) <= 10:
            raise ConfigError(f"scoring.{name} must be 1-10")
    if not 5 <= s.x_api.poll.max_results <= 100:
        raise ConfigError("x_api.poll.max_results must be 5-100")
    if not 0 <= s.x_api.stream.backfill_minutes <= 5:
        raise ConfigError("x_api.stream.backfill_minutes must be 0-5")


# --------------------------------------------------------------------------- watchlist


def _norm_ticker(t: Any) -> str:
    t = str(t).strip().upper().lstrip("$")
    if not TICKER_RE.match(t):
        raise ConfigError(f"invalid ticker {t!r}")
    return t


def load_watchlist(path: str | Path) -> Watchlist:
    raw = YAML(typ="safe").load(Path(path).read_text(encoding="utf-8")) or {}

    # Named ticker groups, e.g. big_tech: [AAPL, MSFT, ...]; referenced as "@big_tech".
    groups: dict[str, list[str]] = {
        str(k): [_norm_ticker(t) for t in v or []] for k, v in (raw.get("ticker_groups") or {}).items()
    }

    def expand(items: list[Any] | None) -> tuple[str, ...]:
        out: list[str] = []
        for it in items or []:
            s = str(it).strip()
            if s.startswith("@"):
                if s[1:] not in groups:
                    raise ConfigError(f"unknown ticker group {s!r}")
                out.extend(groups[s[1:]])
            else:
                out.append(_norm_ticker(s))
        return tuple(dict.fromkeys(out))

    accounts: list[Account] = []
    seen: set[str] = set()
    for i, a in enumerate(raw.get("accounts") or []):
        handle = str(a.get("handle", "")).strip().lstrip("@")
        if not HANDLE_RE.match(handle):
            raise ConfigError(f"accounts[{i}]: invalid handle {handle!r}")
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
                tickers=expand(a.get("tickers")),
                kind=kind,
                expect_name=a.get("expect_name"),
                user_id=user_id or None,
                always_alert=bool(a.get("always_alert", False)),
                include_replies=bool(a.get("include_replies", True)),
                include_retweets=bool(a.get("include_retweets", False)),
                est_posts_per_day=float(a.get("est_posts_per_day", 3.0)),
                notes=str(a.get("notes", "") or ""),
            )
        )
    resolved_accounts = accounts

    kw_rules: list[KeywordRule] = []
    for i, k in enumerate(raw.get("keyword_rules") or []):
        tickers_cfg = k.get("tickers", "all")
        kw_rules.append(
            KeywordRule(
                name=str(k.get("name") or f"keywords-{i}"),
                tickers=("__ALL__",) if tickers_cfg == "all" else expand(tickers_cfg),
                min_followers=int(k.get("min_followers", 1_000_000)),
                extra_operators=str(k.get("extra_operators", "-is:retweet -is:reply")),
                est_posts_per_day=float(k.get("est_posts_per_day", 50.0)),
            )
        )

    my = list(expand(raw.get("my_tickers") or []))
    for a in resolved_accounts:
        my.extend(a.tickers)
    for k in kw_rules:
        if k.tickers != ("__ALL__",):
            my.extend(k.tickers)
    my_tickers = tuple(dict.fromkeys(my))
    # "all" keyword rules mean "every ticker I care about".
    kw_rules = [
        KeywordRule(k.name, my_tickers, k.min_followers, k.extra_operators, k.est_posts_per_day)
        if k.tickers == ("__ALL__",)
        else k
        for k in kw_rules
    ]

    company_names = {
        _norm_ticker(t): tuple(str(n) for n in names or [])
        for t, names in (raw.get("company_names") or {}).items()
    }
    return Watchlist(
        accounts=tuple(resolved_accounts),
        keyword_rules=tuple(kw_rules),
        my_tickers=my_tickers,
        company_names=company_names,
    )


# --------------------------------------------------------------------------- secrets


def load_dotenv_if_present(path: str | Path = ".env") -> None:
    """Load KEY=VALUE pairs from .env without overriding real environment variables."""
    try:
        from dotenv import load_dotenv
    except ImportError:  # pragma: no cover - dependency is in requirements
        return
    if Path(path).exists():
        load_dotenv(path, override=False)


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
