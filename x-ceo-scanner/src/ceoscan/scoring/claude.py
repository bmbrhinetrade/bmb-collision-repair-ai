"""Stage 2 scoring with Claude, plus the deterministic fallbacks.

`ClaudeScorer.score` never raises: on any failure (timeout, rate limit, refusal, truncated
or unparseable output, missing key) it returns `fallback_score(...)` built from the fast
rules, with `error` set, so an outage at Anthropic can delay nothing but the score quality.
All attempts for one post share one time budget (``scoring.total_budget_seconds``), so a slow
or hanging API costs at most that long before the rules-only score is used.

Rules-only scores are deliberately conservative: they are based on the author's own words and
capped at ``scoring.fallback_max_impact`` (default 7 = the alert threshold), so keywords alone
never produce an "urgent" alert, an SMS, or a quiet-hours wake-up.
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
from ..models import Account, Direction, Post, RuleResult, Score, ScoreSource
from ..timeutil import now_utc
from .prompt import SCORE_JSON_SCHEMA, SYSTEM_PROMPT, build_user_message

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


def parse_score_response(text: str, my_tickers: tuple[str, ...] = ()) -> Score:
    """Parse Claude's JSON answer into a Score (source="claude").

    Raises ScoreParseError when no JSON object is found or a required field is missing.
    """
    obj = _extract_json_object(text)
    missing = [k for k in REQUIRED_FIELDS if k not in obj]
    if missing:
        raise ScoreParseError(f"missing required field(s): {', '.join(missing)}")
    return Score(
        tickers=normalize_tickers(obj["tickers"], my_tickers),
        direction=normalize_direction(obj["direction"]),
        impact=_parse_impact(obj["impact_score"]),
        reason=clean_reason(obj["reason"]),
        is_joke=_parse_bool(obj["is_joke_or_meme"]),
        source="claude",
    )


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


def fallback_score(
    rules: RuleResult,
    account: Account | None,
    error: str | None,
    source: ScoreSource = "rules_fallback",
    max_impact: int | None = None,
) -> Score:
    """Score derived from the fast rules only (Claude unavailable, disabled, failed, or skipped).

    ``max_impact`` is ``scoring.fallback_max_impact`` (default 7); see `fallback_impact`.
    """
    cap = DEFAULT_FALLBACK_MAX_IMPACT if max_impact is None else int(max_impact)
    if rules.reasons:
        context_only = rules.flagged and not rules.own_flagged
        prefix = "Fast rules (quoted/parent post only): " if context_only else "Fast rules: "
        reason = prefix + "; ".join(rules.reasons[:5])
    else:
        reason = "Fast rules: no market keywords matched"
    return Score(
        tickers=_fallback_tickers(rules, account),
        direction="unclear",
        impact=fallback_impact(rules, cap),
        reason=clean_reason(reason),
        is_joke=False,
        source=source,
        error=error,
    )


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
    ) -> Score:
        """Score one post. Never raises (except for task cancellation).

        ``now`` (default: the current time) is shown to Claude so it can judge staleness.
        """
        started = time.perf_counter()

        if not self.settings.enabled:
            return self._fallback(post, rules, account, "Claude scoring disabled in settings", started, log=False)
        if self._client is None:
            return self._fallback(post, rules, account, "Claude unavailable: no ANTHROPIC_API_KEY configured", started)

        try:
            kwargs = self._request_kwargs(build_user_message(post, account, rules, my_tickers, tz, now=now))
        except Exception as exc:  # never raise from score(): e.g. malformed post data
            return self._fallback(post, rules, account, f"could not build prompt: {type(exc).__name__}", started)

        response, error = await self._call_within_budget(kwargs, post.id, started)
        if response is None:
            return self._fallback(post, rules, account, error, started)
        return self._score_from_response(response, post, rules, account, my_tickers, started)

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
    ) -> Score:
        fb = fallback_score(rules, account, error, max_impact=self.settings.fallback_max_impact)
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
    ) -> Score:
        usage = getattr(response, "usage", None)
        stop_reason = getattr(response, "stop_reason", None)
        if stop_reason == "refusal":
            details = getattr(response, "stop_details", None)
            category = getattr(details, "category", None) if details is not None else None
            error = f"Claude refused (stop_reason=refusal, category={category or 'unknown'})"
            return self._fallback(post, rules, account, error, started, usage)
        if stop_reason in ("max_tokens", "model_context_window_exceeded"):
            error = f"Claude output truncated (stop_reason={stop_reason})"
            return self._fallback(post, rules, account, error, started, usage)

        # Read blocks by type: with adaptive thinking the first block can be a thinking block.
        text = "".join(
            getattr(block, "text", "") or ""
            for block in (getattr(response, "content", None) or [])
            if getattr(block, "type", None) == "text"
        )
        try:
            score = parse_score_response(text, my_tickers)
        except ScoreParseError as exc:
            return self._fallback(post, rules, account, f"unparseable Claude output: {exc}", started, usage)

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
    ) -> Score:
        cap = self.settings.fallback_max_impact if self.settings is not None else None
        sc = fallback_score(rules, account, None, source="offline", max_impact=cap)
        sc.latency_ms = 0
        return sc

    async def warmup(self) -> str | None:
        """Nothing to check or warm up."""
        return None

    async def aclose(self) -> None:
        return None
