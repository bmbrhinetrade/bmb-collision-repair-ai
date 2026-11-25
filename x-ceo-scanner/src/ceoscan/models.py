"""Shared data models. Every module imports its data shapes from here.

Keep this file dependency-free (stdlib only) so tests and tools can import it cheaply.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Literal

Direction = Literal["bullish", "bearish", "unclear"]
ScoreSource = Literal["claude", "rules_fallback", "offline", "skipped"]
PostSource = Literal["stream", "poll", "recovery", "replay"]
Priority = Literal["low", "normal", "high", "urgent"]

# Red flags / false news (see RedFlagSettings in config.py and the README section "Red flags and false news").
RED_FLAG_TYPES: tuple[str, ...] = (
    "short_report",  # short-seller report or public short position
    "fraud_accounting",  # fraud allegations, accounting irregularities, restatement, auditor exit, going concern
    "legal_regulatory",  # SEC/DOJ/FTC/state probes, subpoenas, Wells notices, lawsuits, charges, fines
    "product_safety",  # recalls, safety defects, FDA warning letters, crashes
    "financial_warning",  # guidance cut, profit warning, missed payments, bankruptcy, delisting, dilution
    "executive",  # sudden CEO/CFO exit, misconduct, insider dumping
    "analyst_downgrade",  # downgrade, price-target cut, initiation at sell
    "security_incident",  # hack, data breach, outage
    "account_compromise",  # a watched company/CEO account posting scam-like content (likely hijacked)
    "clinical_setback",  # failed trial / missed endpoint, FDA complete response letter, clinical hold
    "other",
)
ClaimStatus = Literal["confirmed_official", "credible_report", "unverified", "likely_false", "denial", "not_applicable"]
CredibilityLevel = Literal["official", "trusted", "established", "unverified", "suspicious"]
Risk = Literal["low", "medium", "high"]
SourceKind = Literal["short_seller", "regulator", "newswire", "journalist", "other"]
# "" = not a red-flag alert; "red_flag" = one bad-news post; "spreading" = several authors in a window;
# "denial" = a watched account denies a recent red flag; "compromise" = a watched account looks hijacked.
RedFlagKind = Literal["", "red_flag", "spreading", "denial", "compromise"]


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
    # A second OR-group that must ALSO match, e.g. red-flag words ("short report", lawsuit, recall).
    require_any: tuple[str, ...] = ()
    # Restrict the rule to these authors (numeric X user ids, resolved from the watchlist's `sources`).
    from_ids: tuple[str, ...] = ()
    # "keyword" (default), "red_flag" (tickers AND red-flag words) or "source" (sources AND tickers).
    kind: Literal["keyword", "red_flag", "source"] = "keyword"


@dataclass(frozen=True)
class Source:
    """An account watched ONLY for its posts that mention my tickers or companies (not all its posts).

    Short sellers, regulators, fast news wires and journalists: where bad news often appears first.
    Verified like accounts (`verify --write` stores the numeric user_id).
    """

    handle: str
    kind: SourceKind = "other"
    expect_name: str | None = None
    user_id: str | None = None
    trusted: bool = False  # regulators / established wires: what they post counts as a credible report
    est_posts_per_day: float = 2.0  # matching posts only (for the cost estimate)
    notes: str = ""

    @property
    def handle_lower(self) -> str:
        return self.handle.lower()


@dataclass(frozen=True)
class Watchlist:
    accounts: tuple[Account, ...]
    keyword_rules: tuple[KeywordRule, ...]
    my_tickers: tuple[str, ...]  # union of all tickers I care about (uppercase, no "$")
    company_names: dict[str, tuple[str, ...]]  # ticker -> names/aliases for fast rules
    sources: tuple[Source, ...] = ()  # red-flag sources (see Source)

    def by_user_id(self) -> dict[str, Account]:
        return {a.user_id: a for a in self.accounts if a.user_id}

    def sources_by_user_id(self) -> dict[str, Source]:
        return {s.user_id: s for s in self.sources if s.user_id}

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
    author_verified_type: str | None = None  # "blue" | "business" | "government" | "none"
    author_created_at: datetime | None = None  # account creation time (UTC)
    author_parody: bool | None = None
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
    # Tickers named in the author's OWN words ($cashtags, bare tickers, company names): a denial or a rules-only
    # red flag is about these, never about a ticker only the quoted / parent post names.
    own_tickers: list[str] = field(default_factory=list)


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
    # Red flags / false news (Claude fills these; the rules-only fallback derives them from categories).
    red_flag: bool = False  # the post reports or alleges a negative risk event for a company
    red_flag_types: list[str] = field(default_factory=list)  # values from RED_FLAG_TYPES
    claim_status: ClaimStatus = "not_applicable"
    false_news_risk: Risk = "low"
    credibility_note: str = ""  # one sentence: who says it and how believable it is


@dataclass
class Credibility:
    """Who posted it and how believable the source is - computed in code from author metadata."""

    level: CredibilityLevel
    reasons: list[str] = field(default_factory=list)  # human readable, e.g. "business-verified, 2.1M followers"
    is_official: bool = False  # the author is a watched company / CEO account
    is_trusted_source: bool = False  # a trusted `sources` entry (regulator / wire); a link alone never sets it
    source_kind: str | None = None  # Source.kind when the author is a listed source
    possible_impersonation: bool = False  # display name / handle imitates a watched company or person
    possible_compromise: bool = False  # a watched account posting scam-like content (likely hijacked)
    link_domains: list[str] = field(default_factory=list)  # domains of links in the post (expanded urls)


@dataclass
class RedFlagSignal:
    """What the red-flag tracker knows about one ticker's bad news around one alert."""

    ticker: str
    distinct_authors: int = 0  # distinct authors posting red flags about this ticker in the window
    window_minutes: int = 60
    spreading: bool = False  # distinct_authors reached red_flags.spread_min_authors
    confirmed_by: list[str] = field(default_factory=list)  # official / trusted handles that reported it
    denied_by: list[str] = field(default_factory=list)  # official handles that denied a recent red flag
    related_post_ids: list[str] = field(default_factory=list)


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
    # Red flags / false news (empty / None for ordinary alerts)
    red_flag_kind: RedFlagKind = ""
    claim_status: str | None = None  # Score.claim_status of the alerted post
    credibility: Credibility | None = None
    red_flag_signal: RedFlagSignal | None = None

    @property
    def sent_ok(self) -> bool:
        return any(r.ok for r in self.results)


# --------------------------------------------------------------------------- runtime status

_HTTP_401_RE = re.compile(r"\bHTTP 401\b")


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

    def x_auth_rejected(self) -> bool:
        """The last X error was HTTP 401: X refuses the bearer token (usually regenerated or revoked at
        console.x.com - regenerating a token cancels the old one). ``last_error`` holds ``str(XApiError)``
        ("HTTP 401 Unauthorized: ..."), optionally behind a prefix such as "stream rule sync failed: "."""
        return bool(self.last_error and _HTTP_401_RE.search(self.last_error))
