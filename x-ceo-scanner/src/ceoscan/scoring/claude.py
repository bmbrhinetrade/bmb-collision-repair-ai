"""Stage 2 scoring with Claude, plus the deterministic fallbacks.

`ClaudeScorer.score` never raises: on any failure (timeout, rate limit, refusal, truncated
or unparseable output, missing key) it returns `fallback_score(...)` built from the fast
rules, with `error` set, so an outage at Anthropic can delay nothing but the score quality.
All attempts for one post share one time budget (``scoring.total_budget_seconds``), so a slow
or hanging API costs at most that long before the rules-only score is used.

Rules-only scores are deliberately conservative: they are based on the author's own words and
capped at ``scoring.fallback_max_impact`` (default 7 = the alert threshold), so keywords alone
never produce an "urgent" alert, an SMS, or a quiet-hours wake-up.

Red flags (when the caller passes a ``credibility``, i.e. red flags are on): Claude fills ``red_flag``,
``red_flag_types``, ``claim_status``, ``false_news_risk`` and ``credibility_note``; missing or invalid values get
tolerant defaults (the pipeline then caps the claim by the credibility computed in code, see
scoring/credibility.py ``clamp_claim``). The rules-only fallback derives them from scoring/redflags.py (unhedged
negative events in the author's own words) and the claim status from the credibility level:

* a watched account: CONFIRMED (official) only when the event is about its own company - its own tickers or company
  names in its own words, or a first-person sentence ("we are recalling", "I'm stepping down"); a red flag about
  another of my companies it names is UNVERIFIED and carries only those tickers (Elon on Apple is an allegation
  about Apple, not a Tesla red flag); anything else ("massive fraud in government spending", "X Corp has filed a
  lawsuit against ...") is not a rules-only red flag at all;
* trusted -> credible_report (unverified when the post uses rumor words), a possible impersonator -> likely_false,
  anything else -> unverified (false-news risk high for suspicious authors).

A red flag naming the company gets a rules-only impact of at least :data:`RED_FLAG_FALLBACK_IMPACT` (5 for
suspicious authors), still capped at ``scoring.fallback_max_impact``.
"""

from __future__ import annotations

import asyncio
import json
import logging
import math
import re
import time
from datetime import datetime
from typing import TYPE_CHECKING, Any

import anthropic

from ..config import ScoringSettings
from ..models import (
    RED_FLAG_TYPES,
    Account,
    ClaimStatus,
    Credibility,
    Direction,
    Post,
    RedFlagSignal,
    Risk,
    RuleResult,
    Score,
    ScoreSource,
)
from ..timeutil import now_utc
from .prompt import (
    CLAIM_STATUSES,
    MAX_CREDIBILITY_NOTE_CHARS,
    RISK_LEVELS,
    SCORE_JSON_SCHEMA,
    SYSTEM_PROMPT,
    build_user_message,
)
from .redflags import scan
from .rules import CAT_COMPANY, CAT_TICKER, own_text

if TYPE_CHECKING:  # pragma: no cover
    from anthropic import AsyncAnthropic

logger = logging.getLogger(__name__)

REQUIRED_FIELDS: tuple[str, ...] = ("tickers", "direction", "impact_score", "reason", "is_joke_or_meme")
MAX_REASON_CHARS = 240
_TICKER_RE = re.compile(r"^[A-Z][A-Z0-9.]{0,9}$")
_FENCE_RE = re.compile(r"```[ \t]*(?:json|JSON)?[ \t]*\r?\n?(.*?)```", re.DOTALL)
_LEADING_NUMBER_RE = re.compile(r"^\s*([+-]?\d+(?:\.\d+)?)")

_BULLISH = frozenset({"bull", "bullish", "positive", "up", "long", "buy"})
_BEARISH = frozenset({"bear", "bearish", "negative", "down", "short", "sell"})
_TRUE_STRINGS = frozenset({"true", "yes", "y", "1"})


class ScoreParseError(ValueError):
    """Claude's output could not be turned into a Score."""


# --------------------------------------------------------------------------- parsing


def _extract_json_object(text: str) -> dict[str, Any]:
    """Find the score object in raw JSON, a ``` fence, or JSON surrounded by prose."""
    stripped = text.strip()
    if not stripped:
        raise ScoreParseError("empty response")
    candidates: list[str] = [stripped]
    candidates.extend(m.group(1).strip() for m in _FENCE_RE.finditer(stripped))
    for cand in candidates:
        try:
            obj = json.loads(cand)
        except ValueError:
            continue
        if isinstance(obj, dict):
            return obj
    # Prose around the JSON: try every "{" until one decodes to an object.
    decoder = json.JSONDecoder()
    first_object: dict[str, Any] | None = None
    for m in re.finditer(r"\{", stripped):
        try:
            obj, _ = decoder.raw_decode(stripped, m.start())
        except ValueError:
            continue
        if isinstance(obj, dict):
            if any(k in obj for k in REQUIRED_FIELDS):
                return obj
            if first_object is None:
                first_object = obj
    if first_object is not None:
        return first_object
    raise ScoreParseError("no JSON object found in response")


def _parse_impact(value: Any) -> int:
    if isinstance(value, bool) or value is None:
        raise ScoreParseError(f"impact_score must be a number, got {value!r}")
    number: float
    if isinstance(value, (int, float)):
        number = float(value)
    elif isinstance(value, str):
        m = _LEADING_NUMBER_RE.match(value)
        if not m:
            raise ScoreParseError(f"impact_score is not numeric: {value[:20]!r}")
        number = float(m.group(1))
    else:
        raise ScoreParseError(f"impact_score has unsupported type {type(value).__name__}")
    if math.isnan(number) or math.isinf(number):
        raise ScoreParseError("impact_score is not finite")
    return max(1, min(10, int(round(number))))


def normalize_direction(value: Any) -> Direction:
    """bull/bullish/positive -> bullish, bear/bearish/negative -> bearish, else unclear."""
    if not isinstance(value, str):
        return "unclear"
    v = value.strip().lower()
    if v in _BULLISH:
        return "bullish"
    if v in _BEARISH:
        return "bearish"
    return "unclear"


def normalize_tickers(value: Any, my_tickers: tuple[str, ...] = ()) -> list[str]:
    """Uppercase, strip "$", keep valid symbols, dedupe, my tickers first (stable order)."""
    if value is None:
        return []
    if isinstance(value, str):
        items: list[Any] = re.split(r"[\s,;/]+", value)
    elif isinstance(value, (list, tuple)):
        items = list(value)
    else:
        raise ScoreParseError(f"tickers must be a list, got {type(value).__name__}")
    seen: list[str] = []
    for item in items:
        if not isinstance(item, str):
            continue
        t = item.strip().lstrip("$").strip().upper()
        if _TICKER_RE.match(t) and t not in seen:
            seen.append(t)
    mine = {t.upper() for t in my_tickers}
    return [t for t in seen if t in mine] + [t for t in seen if t not in mine]


def clean_reason(value: Any) -> str:
    """One line, collapsed whitespace, at most MAX_REASON_CHARS characters."""
    if value is None:
        return ""
    text = " ".join(str(value).split())
    if len(text) > MAX_REASON_CHARS:
        text = text[: MAX_REASON_CHARS - 1].rstrip() + "…"
    return text


def _parse_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0
    if isinstance(value, str):
        return value.strip().lower() in _TRUE_STRINGS
    return False


def normalize_red_flag_types(value: Any) -> list[str]:
    """Known RED_FLAG_TYPES only (case / spaces / dashes tolerated), deduped, in the contract's order."""
    if isinstance(value, str):
        items: list[Any] = re.split(r"[\s,;/]+", value)
    elif isinstance(value, (list, tuple)):
        items = list(value)
    else:
        return []
    found = {str(v).strip().lower().replace("-", "_").replace(" ", "_") for v in items if isinstance(v, str)}
    return [t for t in RED_FLAG_TYPES if t in found]


def _enum(value: Any, allowed: tuple[str, ...]) -> str | None:
    if not isinstance(value, str):
        return None
    v = value.strip().lower().replace("-", "_").replace(" ", "_")
    return v if v in allowed else None


def clean_note(value: Any) -> str:
    """One line, at most MAX_CREDIBILITY_NOTE_CHARS characters ("" for anything that is not text)."""
    if not isinstance(value, str):
        return ""
    text = " ".join(value.split())
    if len(text) > MAX_CREDIBILITY_NOTE_CHARS:
        text = text[: MAX_CREDIBILITY_NOTE_CHARS - 1].rstrip() + "…"
    return text


def apply_red_flag_fields(score: Score, obj: dict[str, Any]) -> Score:
    """Fill the red-flag fields of ``score`` from Claude's object, with tolerant defaults.

    Missing / invalid values never fail the parse: red_flag false, types [], claim not_applicable, risk low.
    Inconsistencies are resolved conservatively: a denial is never a red flag; a red flag without a valid
    claim status is "unverified"; a red flag without a type is "other"; a likely-false claim without a valid
    risk is "high" risk.
    """
    red_flag = _parse_bool(obj.get("red_flag", False))
    types = normalize_red_flag_types(obj.get("red_flag_types"))
    claim = _enum(obj.get("claim_status"), CLAIM_STATUSES)
    risk = _enum(obj.get("false_news_risk"), RISK_LEVELS)
    if claim == "denial":
        red_flag = False
    if red_flag:
        types = types or ["other"]
        if claim in (None, "not_applicable"):
            claim = "unverified"
    elif claim != "denial":
        types = []  # (a claim status on good news - "unverified" rumor of a deal - is kept as Claude said)
    if risk is None:
        risk = "high" if claim == "likely_false" else "medium" if red_flag and claim == "unverified" else "low"
    score.red_flag = red_flag
    score.red_flag_types = types
    score.claim_status = (claim or "not_applicable")  # type: ignore[assignment]
    score.false_news_risk = risk  # type: ignore[assignment]
    score.credibility_note = clean_note(obj.get("credibility_note"))
    return score


def parse_score_response(text: str, my_tickers: tuple[str, ...] = ()) -> Score:
    """Parse Claude's JSON answer into a Score (source="claude").

    Raises ScoreParseError when no JSON object is found or a required field is missing. The red-flag
    fields are optional here (tolerant defaults, see :func:`apply_red_flag_fields`).
    """
    obj = _extract_json_object(text)
    missing = [k for k in REQUIRED_FIELDS if k not in obj]
    if missing:
        raise ScoreParseError(f"missing required field(s): {', '.join(missing)}")
    score = Score(
        tickers=normalize_tickers(obj["tickers"], my_tickers),
        direction=normalize_direction(obj["direction"]),
        impact=_parse_impact(obj["impact_score"]),
        reason=clean_reason(obj["reason"]),
        is_joke=_parse_bool(obj["is_joke_or_meme"]),
        source="claude",
    )
    return apply_red_flag_fields(score, obj)


# --------------------------------------------------------------------------- fallback

#: scoring.fallback_max_impact default; callers should pass the configured value
DEFAULT_FALLBACK_MAX_IMPACT: int = ScoringSettings().fallback_max_impact


def fallback_impact(rules: RuleResult, max_impact: int = DEFAULT_FALLBACK_MAX_IMPACT) -> int:
    """Rules-only impact (1..max_impact), driven by the author's OWN words.

    The own-text score is the base. Quoted / parent / reposted context may lift it by half of
    what it adds (rounded up), and never past ``max_impact``: "lol" quoting an earnings recap
    (context 10, own 0) gives 5, a fact-heavy own post keeps its score. With the default cap of 7
    (= the alert threshold) a rules-only score can still alert in the daytime as "high", but never
    reaches "urgent", SMS, quiet hours or Pushover's high priority.
    """
    cap = max(1, min(10, int(max_impact)))
    own = max(0, int(rules.own_rule_score)) if rules.own_flagged else 0
    context = max(own, int(rules.rule_score))
    lifted = own + math.ceil((context - own) / 2)
    return max(1, min(cap, lifted))


def _fallback_tickers(rules: RuleResult, account: Account | None) -> list[str]:
    """Tickers for a rules-only score.

    Explicit $cashtags / tickers as detected. When the rules only matched company-name aliases
    and the author is a watched account, the aliases that belong to the account come first
    (@elonmusk "Starship ..." -> SPCX); if none does ("Gemini" in an @elonmusk post), the
    account's mapped tickers come first, so a common-word alias cannot replace them.
    """
    rule_tickers = list(rules.tickers)
    acct = list(account.tickers) if account is not None else []
    if not rule_tickers:
        return acct
    if not acct or "ticker" in rules.categories:
        return rule_tickers
    shared = [t for t in rule_tickers if t in acct]
    if shared:
        return shared + [t for t in rule_tickers if t not in shared]
    return acct + [t for t in rule_tickers if t not in acct]


#: rules-only impact floor for a red flag that names the company (= red_flags.min_score by default), still capped
#: at scoring.fallback_max_impact; suspicious authors (new / parody accounts) get one less, so their rumors alone do
#: not alert rules-only (the "spreading" alert still counts them). Possible impersonators keep the full floor:
#: a fake CEO / company post is exactly the false news that moves a stock (Eli Lilly, Nov 2022).
RED_FLAG_FALLBACK_IMPACT = 6
_CLAIM_BY_LEVEL: dict[str, ClaimStatus] = {
    "official": "confirmed_official",
    "trusted": "credible_report",
    "established": "unverified",
    "unverified": "unverified",
    "suspicious": "unverified",
}
_RISK_BY_LEVEL: dict[str, Risk] = {
    "official": "low",
    "trusted": "low",
    "established": "medium",
    "unverified": "medium",
    "suspicious": "high",
}


#: rumor markers that make even an official / trusted claim unverified ("leaked" alone does not: "our data leaked")
_STRONG_RUMOR = frozenset({"rumor", "unconfirmed", "reportedly", "anonymous sources", "hearing that", "allegedly",
                           "cannot confirm"})


def _rules_only_note(credibility: Credibility, claim: str, *, other_company: bool = False) -> str:
    """A short rules-only credibility note (the alert lists the credibility reasons separately)."""
    if claim == "denial":
        return "Rules-only: a watched account denies a negative claim."
    if claim == "confirmed_official":
        return "Rules-only: posted by the company / CEO account itself."
    if other_company:
        return "Rules-only: a watched account makes a claim about another company; the claim was not checked."
    if credibility.possible_impersonation:
        return "Rules-only: the author imitates a watched account; the claim is likely false."
    return f"Rules-only: {credibility.level} source; the claim was not checked."


def _norm_tickers(tickers: list[str] | tuple[str, ...]) -> list[str]:
    return list(dict.fromkeys(str(t).strip().lstrip("$").upper() for t in tickers if str(t).strip()))


def apply_rules_only_red_flag(
    score: Score, post: Post, rules: RuleResult, account: Account | None, credibility: Credibility, max_impact: int
) -> Score:
    """Derive the red-flag fields of a rules-only score from the post's own words and the author's credibility.

    See the module docstring: a watched account's post is CONFIRMED only about its own company.
    """
    own = own_text(post)
    found = scan(own, has_link=bool(post.urls))
    if credibility.possible_compromise:
        score.red_flag, score.red_flag_types = True, ["account_compromise"]
        score.claim_status, score.false_news_risk = "likely_false", "high"
        score.direction = "unclear"  # a scam post: do not act on it either way
        score.credibility_note = clean_note(
            "Rules-only: a watched account posted scam-like crypto content; it may be hijacked."
        )
        return score
    if found.denial:
        score.red_flag, score.red_flag_types = False, found.types
        score.claim_status, score.false_news_risk = "denial", "low"
        score.credibility_note = _rules_only_note(credibility, "denial")
        return score
    types = found.event_types(by_company=account is not None)
    if not types:
        return score
    claim: ClaimStatus = _CLAIM_BY_LEVEL.get(credibility.level, "unverified")
    risk: Risk = _RISK_BY_LEVEL.get(credibility.level, "medium")
    named = _norm_tickers(rules.own_tickers)
    tickers: list[str] | None = None
    other_company = False
    if account is not None:
        # a watched account: whose bad news is it? (never "every post of the account is about its company")
        acct = _norm_tickers(account.tickers)
        own_named = [t for t in named if t in acct]
        others = [t for t in named if t not in acct]
        if own_named and not others:
            claim, risk, tickers = "confirmed_official", "low", own_named
        elif named:
            claim, risk, tickers, other_company = "unverified", "medium", named, True
        elif found.first_person_types:
            # "we" / "I" naming no company: the account's primary (first) ticker, not a whole ticker group
            claim, risk, tickers, types = "confirmed_official", "low", acct[:1], found.first_person_types
        else:
            return score  # names no company and is not about itself: keep the ordinary decision
        names_company = True
    else:
        if credibility.possible_impersonation:
            claim, risk = "likely_false", "high"
        names_company = bool({CAT_TICKER, CAT_COMPANY} & set(rules.own_categories))
    if claim in ("confirmed_official", "credible_report") and found.rumor:
        strong_rumor = bool(_STRONG_RUMOR & set(found.rumor))
        if claim == "credible_report" or strong_rumor:
            claim, risk = "unverified", "medium"  # "rumor, unconfirmed, sources say" is never a credible report
    score.red_flag, score.red_flag_types = True, types
    score.claim_status, score.false_news_risk = claim, risk
    score.direction = "bearish"
    score.credibility_note = _rules_only_note(credibility, claim, other_company=other_company)
    if tickers is not None:
        score.tickers = tickers
    if names_company:
        weak_author = credibility.level == "suspicious" and not credibility.possible_impersonation
        floor = RED_FLAG_FALLBACK_IMPACT - (1 if weak_author else 0)
        score.impact = max(int(score.impact), min(floor, max(1, int(max_impact))))
    return score


def fallback_score(
    rules: RuleResult,
    account: Account | None,
    error: str | None,
    source: ScoreSource = "rules_fallback",
    max_impact: int | None = None,
    *,
    post: Post | None = None,
    credibility: Credibility | None = None,
) -> Score:
    """Score derived from the fast rules only (Claude unavailable, disabled, failed, or skipped).

    ``max_impact`` is ``scoring.fallback_max_impact`` (default 7); see `fallback_impact`. With ``post`` and
    ``credibility`` (red flags on) the red-flag fields are derived too (:func:`apply_rules_only_red_flag`).
    """
    cap = DEFAULT_FALLBACK_MAX_IMPACT if max_impact is None else int(max_impact)
    if rules.reasons:
        context_only = rules.flagged and not rules.own_flagged
        prefix = "Fast rules (quoted/parent post only): " if context_only else "Fast rules: "
        reason = prefix + "; ".join(rules.reasons[:5])
    else:
        reason = "Fast rules: no market keywords matched"
    score = Score(
        tickers=_fallback_tickers(rules, account),
        direction="unclear",
        impact=fallback_impact(rules, cap),
        reason=clean_reason(reason),
        is_joke=False,
        source=source,
        error=error,
    )
    if post is not None and credibility is not None:
        apply_rules_only_red_flag(score, post, rules, account, credibility, cap)
    return score


# --------------------------------------------------------------------------- scorers


def _status_error_text(exc: anthropic.APIStatusError) -> str:
    """'HTTP 429 rate_limit_error: <message>' - API error bodies never contain our key."""
    err_type = None
    message = None
    body = exc.body
    if isinstance(body, dict):
        inner = body.get("error")
        if isinstance(inner, dict):
            err_type = inner.get("type")
            message = inner.get("message")
    parts = f"HTTP {exc.status_code}"
    if err_type:
        parts += f" {err_type}"
    if message:
        parts += f": {' '.join(str(message).split())[:160]}"
    return parts


def _should_retry_header(exc: anthropic.APIStatusError) -> bool | None:
    try:
        value = exc.response.headers.get("x-should-retry")
    except Exception:  # pragma: no cover - defensive: odd response objects
        return None
    if value == "true":
        return True
    if value == "false":
        return False
    return None


def _retry_after_seconds(exc: anthropic.APIStatusError) -> float | None:
    try:
        value = exc.response.headers.get("retry-after")
        return float(value) if value is not None else None
    except (TypeError, ValueError):
        return None
    except Exception:  # pragma: no cover - defensive
        return None


#: HTTP statuses that mean "this request will never work as configured" (key, billing, model,
#: parameters). Retrying is pointless; the operator has to fix settings or the account.
REJECTED_STATUSES: frozenset[int] = frozenset({400, 401, 402, 403, 404, 413, 422})


class ClaudeScorer:
    """Scores posts with Claude (structured JSON output); falls back to fast-rule scores.

    Latency: one post gets ``scoring.total_budget_seconds`` (default 4 s) for ALL attempts
    together, enforced with ``asyncio.timeout``. Each attempt's SDK timeout is the time that is
    left. A timeout is never retried (the next attempt would hit the same wall). A fast failure
    (429, 5xx / 529 overloaded, connection error) is retried, at most ``scoring.max_retries``
    times, only if at least `min_retry_seconds` would remain after the backoff and the server's
    ``retry-after`` fits in the budget. The SDK's own retries are off (``max_retries=0``).
    """

    #: base delay between retries (doubles per attempt); class attributes so tests can zero them
    retry_base_delay: float = 0.25
    #: cap for the exponential backoff (a server retry-after may ask for more if the budget allows)
    retry_max_delay: float = 1.0
    #: retry only if at least this much of the budget is left after the backoff
    min_retry_seconds: float = 1.5
    #: the startup warm-up is not latency-critical: TLS + a possible schema compile
    warmup_timeout_seconds: float = 15.0
    warmup_max_tokens: int = 16
    #: how often the same "request rejected" error is logged at ERROR level (it repeats per post)
    rejection_log_interval: float = 3600.0

    def __init__(
        self,
        settings: ScoringSettings,
        api_key: str | None,
        client: AsyncAnthropic | None = None,
    ) -> None:
        self.settings = settings
        self._owns_client = False
        if client is None and api_key:
            # Our own bounded retry loop replaces the SDK's (default 2 retries, long backoff).
            client = anthropic.AsyncAnthropic(
                api_key=api_key, max_retries=0, timeout=settings.timeout_seconds
            )
            self._owns_client = True
        self._client = client
        #: last non-retryable rejection (HTTP 400/401/403/404, billing ...), None after a success.
        #: Lets the app raise a (throttled) system alert: keys and billing can fail after startup.
        self.last_rejection: str | None = None
        self.last_rejection_at: float | None = None  # time.monotonic()
        #: set by warmup(): True when its error is transient (timeout, 429, 5xx, network)
        self.warmup_transient: bool | None = None
        self._rejection_logged: tuple[str, float] | None = None

    # ------------------------------------------------------------------ request building

    def _request_kwargs(self, user_message: str) -> dict[str, Any]:
        """Messages API arguments, without ``timeout`` (set per attempt from the remaining budget)."""
        s = self.settings
        kwargs: dict[str, Any] = {
            "model": s.model,
            "max_tokens": s.max_tokens,
            "system": SYSTEM_PROMPT,
            "messages": [{"role": "user", "content": user_message}],
            "output_config": {
                "effort": s.effort,
                "format": {"type": "json_schema", "schema": SCORE_JSON_SCHEMA},
            },
        }
        # No temperature/top_p/top_k: rejected by current models / API version.
        if s.thinking == "disabled":
            kwargs["thinking"] = {"type": "disabled"}
        return kwargs

    # ------------------------------------------------------------------ scoring

    async def score(
        self,
        post: Post,
        account: Account | None,
        rules: RuleResult,
        my_tickers: tuple[str, ...],
        tz: str,
        now: datetime | None = None,
        *,
        credibility: Credibility | None = None,
        signal: RedFlagSignal | None = None,
    ) -> Score:
        """Score one post. Never raises (except for task cancellation).

        ``now`` (default: the current time) is shown to Claude so it can judge staleness. ``credibility``
        (red flags on) adds the trusted source facts to the prompt, with ``signal`` (bad news about the ticker
        seen so far) as a hint, and lets a rules-only fallback derive the red-flag fields.
        """
        started = time.perf_counter()
        cred = credibility

        if not self.settings.enabled:
            return self._fallback(
                post, rules, account, "Claude scoring disabled in settings", started, log=False, credibility=cred
            )
        if self._client is None:
            return self._fallback(
                post, rules, account, "Claude unavailable: no ANTHROPIC_API_KEY configured", started, credibility=cred
            )

        try:
            message = build_user_message(
                post, account, rules, my_tickers, tz, now=now, credibility=cred, signal=signal
            )
            kwargs = self._request_kwargs(message)
        except Exception as exc:  # never raise from score(): e.g. malformed post data
            return self._fallback(
                post, rules, account, f"could not build prompt: {type(exc).__name__}", started, credibility=cred
            )

        response, error = await self._call_within_budget(kwargs, post.id, started)
        if response is None:
            return self._fallback(post, rules, account, error, started, credibility=cred)
        return self._score_from_response(response, post, rules, account, my_tickers, started, credibility=cred)

    async def _call_within_budget(self, kwargs: dict[str, Any], post_id: str, started: float) -> tuple[Any, str]:
        """(response, "") on success, (None, error text) otherwise. Never raises except cancellation."""
        assert self._client is not None
        s = self.settings
        budget = max(0.05, float(s.total_budget_seconds))
        per_request = max(0.05, float(s.timeout_seconds))
        attempts = max(0, int(s.max_retries)) + 1
        loop = asyncio.get_running_loop()
        deadline = loop.time() + budget

        def timeout_text() -> str:
            return f"timeout after {time.perf_counter() - started:.1f}s (scoring budget {budget:g}s)"

        last_error = "unknown error"
        try:
            async with asyncio.timeout_at(deadline):
                for attempt in range(attempts):
                    attempt_timeout = max(0.05, min(per_request, deadline - loop.time()))
                    status_exc: anthropic.APIStatusError | None = None
                    retryable = False
                    try:
                        response = await self._client.messages.create(**kwargs, timeout=attempt_timeout)
                        self.last_rejection = None
                        return response, ""
                    # Most specific first: APITimeoutError subclasses APIConnectionError, and
                    # RateLimitError / InternalServerError subclass APIStatusError.
                    except anthropic.APITimeoutError:
                        return None, timeout_text()  # never retry after a timeout
                    except anthropic.RateLimitError as exc:
                        status_exc = exc
                        last_error, retryable = _status_error_text(exc), True
                    except anthropic.InternalServerError as exc:  # >= 500, incl. 529 overloaded
                        status_exc = exc
                        last_error, retryable = _status_error_text(exc), True
                    except anthropic.APIStatusError as exc:  # other statuses: bad request, auth, billing ...
                        status_exc = exc
                        last_error = _status_error_text(exc)
                        retryable = exc.status_code >= 500 or exc.status_code in (408, 409)
                        if exc.status_code in REJECTED_STATUSES:
                            self._note_rejection(last_error)
                    except anthropic.APIConnectionError:
                        last_error, retryable = "connection error", True
                    except anthropic.AnthropicError as exc:  # e.g. response validation errors
                        last_error = f"anthropic error: {type(exc).__name__}"
                    except Exception as exc:  # last resort: score() must never raise
                        logger.exception("unexpected error calling Claude for post %s", post_id)
                        last_error = f"unexpected error: {type(exc).__name__}"

                    delay = min(self.retry_base_delay * (2**attempt), self.retry_max_delay)
                    if status_exc is not None:
                        header = _should_retry_header(status_exc)
                        if header is not None:
                            retryable = header
                        retry_after = _retry_after_seconds(status_exc)
                        if retryable and retry_after is not None:
                            if deadline - loop.time() - retry_after < self.min_retry_seconds:
                                return None, last_error + f" (retry-after {retry_after:g}s)"
                            delay = max(delay, retry_after)
                    if not retryable or attempt + 1 >= attempts:
                        return None, last_error
                    remaining = deadline - loop.time() - delay
                    if remaining < self.min_retry_seconds:
                        return None, last_error  # a retry could not finish within the budget
                    logger.info(
                        "claude attempt %d/%d for post %s failed (%s); retrying in %.2fs (%.1fs of budget left)",
                        attempt + 1,
                        attempts,
                        post_id,
                        last_error,
                        delay,
                        remaining,
                    )
                    if delay > 0:
                        await asyncio.sleep(delay)
        except TimeoutError:  # the overall budget expired (raised by asyncio.timeout_at)
            return None, timeout_text()
        return None, last_error  # pragma: no cover - the loop always returns

    def _note_rejection(self, error: str) -> None:
        now = time.monotonic()
        self.last_rejection, self.last_rejection_at = error, now
        prev = self._rejection_logged
        if prev is None or prev[0] != error or now - prev[1] >= self.rejection_log_interval:
            self._rejection_logged = (error, now)
            logger.error("claude request rejected (%s); check scoring settings, API key and billing", error)

    def _fallback(
        self,
        post: Post,
        rules: RuleResult,
        account: Account | None,
        error: str,
        started: float,
        usage: Any = None,
        log: bool = True,
        credibility: Credibility | None = None,
    ) -> Score:
        fb = fallback_score(
            rules, account, error, max_impact=self.settings.fallback_max_impact, post=post, credibility=credibility
        )
        fb.latency_ms = int((time.perf_counter() - started) * 1000)
        if usage is not None:  # refusals / truncations are still billed
            fb.input_tokens = getattr(usage, "input_tokens", None)
            fb.output_tokens = getattr(usage, "output_tokens", None)
        if log:
            logger.warning("claude scoring failed for post %s, using fast-rules score: %s", post.id, error)
        return fb

    def _score_from_response(
        self,
        response: Any,
        post: Post,
        rules: RuleResult,
        account: Account | None,
        my_tickers: tuple[str, ...],
        started: float,
        credibility: Credibility | None = None,
    ) -> Score:
        usage = getattr(response, "usage", None)
        stop_reason = getattr(response, "stop_reason", None)
        if stop_reason == "refusal":
            details = getattr(response, "stop_details", None)
            category = getattr(details, "category", None) if details is not None else None
            error = f"Claude refused (stop_reason=refusal, category={category or 'unknown'})"
            return self._fallback(post, rules, account, error, started, usage, credibility=credibility)
        if stop_reason in ("max_tokens", "model_context_window_exceeded"):
            error = f"Claude output truncated (stop_reason={stop_reason})"
            return self._fallback(post, rules, account, error, started, usage, credibility=credibility)

        # Read blocks by type: with adaptive thinking the first block can be a thinking block.
        text = "".join(
            getattr(block, "text", "") or ""
            for block in (getattr(response, "content", None) or [])
            if getattr(block, "type", None) == "text"
        )
        try:
            score = parse_score_response(text, my_tickers)
        except ScoreParseError as exc:
            error = f"unparseable Claude output: {exc}"
            return self._fallback(post, rules, account, error, started, usage, credibility=credibility)

        score.model = getattr(response, "model", None) or self.settings.model
        score.latency_ms = int((time.perf_counter() - started) * 1000)
        if usage is not None:
            score.input_tokens = getattr(usage, "input_tokens", None)
            score.output_tokens = getattr(usage, "output_tokens", None)
        logger.debug(
            "claude scored post %s impact=%d direction=%s tickers=%s in %d ms",
            post.id,
            score.impact,
            score.direction,
            score.tickers,
            score.latency_ms,
        )
        return score

    # ------------------------------------------------------------------ startup check

    async def warmup(self) -> str | None:
        """Send one tiny scoring request at startup: validates key, model and request parameters
        (e.g. ``thinking: disabled`` on a model that rejects it) and warms TLS and the
        structured-output schema cache, so the first real post does not pay for them.

        Same system prompt, schema and thinking setting as real requests, a synthetic post,
        ``max_tokens`` = `warmup_max_tokens` and effort "low" (well under $0.001).

        Returns None on success (or when scoring is disabled), else a one-line error that never
        contains the API key. Errors starting with "Claude rejected" are configuration problems
        (bad key / model / parameters / billing); for transient ones (timeout, 429, 5xx, network)
        `warmup_transient` is True. Never raises, except for task cancellation.
        """
        self.warmup_transient = None
        if not self.settings.enabled:
            return None
        if self._client is None:
            self.warmup_transient = False
            return "Claude unavailable: no ANTHROPIC_API_KEY configured"
        started = time.perf_counter()
        try:
            now = now_utc()
            post = Post(
                id="0",
                text="Warm-up check: Example Corp ships a minor software update for its mobile app.",
                author_id="0",
                created_at=now,
                received_at=now,
                source="replay",
                author_username="example_corp",
                author_name="Example Corp",
            )
            kwargs = self._request_kwargs(build_user_message(post, None, RuleResult(flagged=False), (), "UTC", now=now))
            kwargs["max_tokens"] = self.warmup_max_tokens
            kwargs["output_config"] = {**kwargs["output_config"], "effort": "low"}
            async with asyncio.timeout(self.warmup_timeout_seconds):  # hard wall over the SDK's timeouts
                response = await self._client.messages.create(**kwargs, timeout=self.warmup_timeout_seconds)
        except (TimeoutError, anthropic.APITimeoutError):
            self.warmup_transient = True
            return f"Claude warm-up timed out after {time.perf_counter() - started:.1f}s"
        except anthropic.APIStatusError as exc:
            text = _status_error_text(exc)
            if exc.status_code in REJECTED_STATUSES:
                self.warmup_transient = False
                self._note_rejection(text)
                return (
                    f"Claude rejected the warm-up request ({text}); check ANTHROPIC_API_KEY, "
                    f"scoring.model ({self.settings.model}), scoring.thinking and billing"
                )
            self.warmup_transient = True
            return f"Claude warm-up failed ({text})"
        except anthropic.APIConnectionError:
            self.warmup_transient = True
            return "Claude warm-up failed (connection error)"
        except anthropic.AnthropicError as exc:
            self.warmup_transient = True
            return f"Claude warm-up failed (anthropic error: {type(exc).__name__})"
        except Exception as exc:  # never raise from warmup()
            logger.debug("unexpected error in Claude warm-up", exc_info=True)
            self.warmup_transient = True
            return f"Claude warm-up failed (unexpected error: {type(exc).__name__})"
        self.last_rejection = None
        logger.info(
            "Claude warm-up ok: %s answered in %d ms (stop_reason=%s)",
            getattr(response, "model", None) or self.settings.model,
            int((time.perf_counter() - started) * 1000),
            getattr(response, "stop_reason", None),
        )
        return None

    async def aclose(self) -> None:
        """Close the HTTP client if this scorer created it."""
        if self._client is not None and self._owns_client:
            try:
                await self._client.close()
            except Exception:  # pragma: no cover - shutdown best effort
                logger.debug("error closing Anthropic client", exc_info=True)


class OfflineScorer:
    """Fast-rules-only scorer with the ClaudeScorer interface (replay, tests, no API key)."""

    def __init__(self, settings: ScoringSettings | None = None) -> None:
        self.settings = settings

    async def score(
        self,
        post: Post,
        account: Account | None,
        rules: RuleResult,
        my_tickers: tuple[str, ...],
        tz: str,
        now: datetime | None = None,
        *,
        credibility: Credibility | None = None,
        signal: RedFlagSignal | None = None,
    ) -> Score:
        cap = self.settings.fallback_max_impact if self.settings is not None else None
        sc = fallback_score(rules, account, None, source="offline", max_impact=cap, post=post, credibility=credibility)
        sc.latency_ms = 0
        return sc

    async def warmup(self) -> str | None:
        """Nothing to check or warm up."""
        return None

    async def aclose(self) -> None:
        return None
