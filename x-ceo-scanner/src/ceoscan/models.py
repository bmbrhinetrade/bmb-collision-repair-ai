"""Shared data models. Every module imports its data shapes from here.

Keep this file dependency-free (stdlib only) so tests and tools can import it cheaply.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Literal

Direction = Literal["bullish", "bearish", "unclear"]
ScoreSource = Literal["claude", "rules_fallback", "offline", "skipped"]
PostSource = Literal["stream", "poll", "recovery", "replay"]
Priority = Literal["low", "normal", "high", "urgent"]


# --------------------------------------------------------------------------- watchlist


@dataclass(frozen=True)
class Account:
    """One X account from config/watchlist.yaml."""

    handle: str  # without "@", as written in the watchlist
    tickers: tuple[str, ...]
    kind: Literal["person", "company"] = "person"
    expect_name: str | None = None  # used by `verify` to flag look-alike accounts
    user_id: str | None = None  # numeric X user id (string), filled in by `verify --write`
    always_alert: bool = False
    include_replies: bool = True
    include_retweets: bool = False
    est_posts_per_day: float = 3.0  # used only by the cost estimator
    notes: str = ""

    @property
    def handle_lower(self) -> str:
        return self.handle.lower()


@dataclass(frozen=True)
class KeywordRule:
    """A handle-less rule: posts mentioning tickers from very large accounts."""

    name: str
    tickers: tuple[str, ...]  # matched as $CASHTAGS in the stream rule
    min_followers: int = 1_000_000
    extra_operators: str = "-is:retweet -is:reply"  # appended verbatim to the rule
    est_posts_per_day: float = 50.0
    # Extra words / exact phrases OR-ed with the cashtags, e.g. ("Apnimed", "AD109", "Talen Energy").
    terms: tuple[str, ...] = ()


@dataclass(frozen=True)
class Watchlist:
    accounts: tuple[Account, ...]
    keyword_rules: tuple[KeywordRule, ...]
    my_tickers: tuple[str, ...]  # union of all tickers I care about (uppercase, no "$")
    company_names: dict[str, tuple[str, ...]]  # ticker -> names/aliases for fast rules

    def by_user_id(self) -> dict[str, Account]:
        return {a.user_id: a for a in self.accounts if a.user_id}

    def by_handle(self) -> dict[str, Account]:
        return {a.handle_lower: a for a in self.accounts}


# --------------------------------------------------------------------------- posts


@dataclass
class ReferencedPost:
    type: Literal["quoted", "replied_to", "retweeted"]
    id: str
    text: str | None = None
    author_id: str | None = None
    author_username: str | None = None


@dataclass
class Post:
    """A normalized X post, regardless of whether it came from stream, poll or replay."""

    id: str
    text: str  # full text (note_tweet.text when present, else text)
    author_id: str
    created_at: datetime  # UTC, millisecond precision (decoded from the snowflake id)
    received_at: datetime  # UTC, when our process received the bytes
    source: PostSource
    author_username: str | None = None
    author_name: str | None = None
    author_followers: int | None = None
    author_verified: bool | None = None
    conversation_id: str | None = None
    in_reply_to_user_id: str | None = None
    lang: str | None = None
    referenced: list[ReferencedPost] = field(default_factory=list)
    cashtags: list[str] = field(default_factory=list)  # uppercase, no "$"
    mentions: list[str] = field(default_factory=list)  # usernames, no "@"
    urls: list[str] = field(default_factory=list)  # expanded urls
    matching_rule_tags: list[str] = field(default_factory=list)
    edit_history_ids: list[str] = field(default_factory=list)
    raw: dict[str, Any] = field(default_factory=dict)

    @property
    def url(self) -> str:
        if self.author_username:
            return f"https://x.com/{self.author_username}/status/{self.id}"
        return f"https://x.com/i/web/status/{self.id}"

    @property
    def is_edit(self) -> bool:
        return len(self.edit_history_ids) > 1

    @property
    def is_reply(self) -> bool:
        return any(r.type == "replied_to" for r in self.referenced)

    @property
    def is_quote(self) -> bool:
        return any(r.type == "quoted" for r in self.referenced)


@dataclass
class IngestItem:
    """What ingest (stream/poller/replay) puts on the pipeline queue."""

    payload: dict[str, Any]  # one stream-shaped JSON object: {"data":..., "includes":..., "matching_rules":...}
    received_at: datetime  # UTC
    source: PostSource


# --------------------------------------------------------------------------- scoring


@dataclass
class RuleResult:
    """Stage 1 (fast rules) output."""

    flagged: bool
    reasons: list[str] = field(default_factory=list)  # human readable, e.g. "cashtag $TSLA", "keyword: guidance"
    categories: list[str] = field(default_factory=list)  # e.g. ["ticker", "earnings", "numbers"]
    tickers: list[str] = field(default_factory=list)  # tickers detected by rules (uppercase)
    has_numbers: bool = False
    rule_score: int = 0  # rough 0-10 heuristic over post + quoted/parent context
    # The same evaluation over the post's OWN text only (no quoted/parent context). The rules-only
    # fallback score is based on this so a bare "lol" quoting an earnings post cannot score 10.
    own_flagged: bool = False
    own_categories: list[str] = field(default_factory=list)
    own_rule_score: int = 0


@dataclass
class Score:
    """Stage 2 (Claude) output, or a fallback derived from the fast rules."""

    tickers: list[str]
    direction: Direction
    impact: int  # 1-10
    reason: str  # one sentence
    is_joke: bool
    source: ScoreSource
    model: str | None = None
    latency_ms: int | None = None
    error: str | None = None  # set when Claude failed and we fell back
    input_tokens: int | None = None
    output_tokens: int | None = None


@dataclass
class Decision:
    should_alert: bool
    reason: str  # e.g. "score 8 >= 7", "always_alert account", "quiet hours: 8 < 9", "below threshold"
    quiet_hours: bool = False
    priority: Priority = "normal"


# --------------------------------------------------------------------------- alerts


@dataclass
class AlertMessage:
    title: str
    body: str
    url: str | None = None
    priority: Priority = "normal"
    tags: list[str] = field(default_factory=list)
    sms_text: str | None = None  # short form for SMS; falls back to title
    kind: Literal["post", "system", "summary"] = "post"


@dataclass
class ChannelResult:
    channel: str  # "pushover", "ntfy", "sms", "email", "console"
    ok: bool
    elapsed_ms: int
    status: int | None = None
    error: str | None = None  # never contains secrets


@dataclass
class AlertRecord:
    post_id: str | None
    kind: Literal["post", "system", "summary"]
    decision_reason: str
    message: AlertMessage
    results: list[ChannelResult]
    score: int | None = None
    direction: str | None = None
    tickers: list[str] = field(default_factory=list)
    quiet_hours: bool = False
    late: bool = False  # post was older than alerts.max_post_age_seconds when received (catch-up)
    # latency breakdown, all milliseconds; None for system alerts
    total_latency_ms: int | None = None  # post created -> first successful push channel returned
    x_delay_ms: int | None = None  # post created -> we received it
    scoring_ms: int | None = None  # rules + Claude
    push_ms: int | None = None  # dispatch -> first successful channel

    @property
    def sent_ok(self) -> bool:
        return any(r.ok for r in self.results)


# --------------------------------------------------------------------------- runtime status


@dataclass
class IngestStatus:
    """Shared, mutable status object. Ingest writes it; health/scheduler read it."""

    mode: Literal["stream", "poll", "replay", "idle"] = "idle"
    state: str = "starting"  # starting|connected|reconnecting|backoff|down|credits_depleted|stopped|polling
    connected_since: datetime | None = None
    last_heartbeat_at: datetime | None = None  # any bytes (incl. keep-alive) received
    last_post_at: datetime | None = None
    last_post_id: str | None = None
    down_since: datetime | None = None  # first moment we were not connected, None while healthy
    reconnects: int = 0
    last_error: str | None = None
    poll_interval_seconds: float | None = None  # per-account cycle when polling
    estimated_latency_seconds: float | None = None
    notes: list[str] = field(default_factory=list)
