"""Stage 2 scoring with Claude, plus the deterministic fallbacks.

`ClaudeScorer.score` never raises: on any failure (timeout, rate limit, refusal, truncated
or unparseable output, missing key) it returns `fallback_score(...)` built from the fast
rules, with `error` set, so an outage at Anthropic can delay nothing but the score quality.
"""

from __future__ import annotations

import asyncio
import json
import logging
import math
import re
import time
from typing import TYPE_CHECKING, Any

import anthropic

from ..config import ScoringSettings
from ..models import Account, Direction, Post, RuleResult, Score, ScoreSource
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


def fallback_score(
    rules: RuleResult,
    account: Account | None,
    error: str | None,
    source: ScoreSource = "rules_fallback",
) -> Score:
    """Score derived from the fast rules only (Claude unavailable, disabled, or failed)."""
    tickers = list(rules.tickers) or (list(account.tickers) if account else [])
    if rules.reasons:
        reason = "Fast rules: " + "; ".join(rules.reasons[:5])
    else:
        reason = "Fast rules: no market keywords matched"
    return Score(
        tickers=tickers,
        direction="unclear",
        impact=max(1, min(10, int(rules.rule_score))),
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


class ClaudeScorer:
    """Scores posts with Claude (structured JSON output); falls back to fast-rule scores."""

    #: base delay between retries (doubles per attempt); class attributes so tests can zero them
    retry_base_delay: float = 0.25
    #: never wait longer than this before a retry - an alert that is late is worth less
    retry_max_delay: float = 1.0
    #: extra wall-clock allowance on top of the SDK timeout before we abandon an attempt
    hard_timeout_slack: float = 0.5

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

    def _request_kwargs(self, user_message: str) -> dict[str, Any]:
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
            "timeout": s.timeout_seconds,
        }
        # No temperature/top_p/top_k: rejected by current models / API version.
        if s.thinking == "disabled":
            kwargs["thinking"] = {"type": "disabled"}
        return kwargs

    async def score(
        self,
        post: Post,
        account: Account | None,
        rules: RuleResult,
        my_tickers: tuple[str, ...],
        tz: str,
    ) -> Score:
        """Score one post. Never raises (except for task cancellation)."""
        started = time.perf_counter()

        if not self.settings.enabled:
            return self._fallback(post, rules, account, "Claude scoring disabled in settings", started, log=False)
        if self._client is None:
            return self._fallback(post, rules, account, "Claude unavailable: no ANTHROPIC_API_KEY configured", started)

        try:
            kwargs = self._request_kwargs(build_user_message(post, account, rules, my_tickers, tz))
        except Exception as exc:  # never raise from score(): e.g. malformed post data
            return self._fallback(post, rules, account, f"could not build prompt: {type(exc).__name__}", started)

        attempts = max(0, int(self.settings.max_retries)) + 1
        hard_timeout = float(self.settings.timeout_seconds) + self.hard_timeout_slack
        timeout_text = f"timeout after {self.settings.timeout_seconds:g}s"
        response: Any = None
        last_error = "unknown error"
        for attempt in range(attempts):
            retryable = False
            status_exc: anthropic.APIStatusError | None = None
            try:
                response = await asyncio.wait_for(self._client.messages.create(**kwargs), timeout=hard_timeout)
                break
            # Most specific first: APITimeoutError subclasses APIConnectionError, and
            # RateLimitError / InternalServerError subclass APIStatusError.
            except anthropic.APITimeoutError:
                last_error, retryable = timeout_text, True
            except TimeoutError:  # our wall-clock guard around the SDK call
                last_error, retryable = timeout_text, True
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
                if exc.status_code in (400, 401, 403, 404):
                    logger.error("claude request rejected (%s); check scoring settings and API key", last_error)
            except anthropic.APIConnectionError:
                last_error, retryable = "connection error", True
            except anthropic.AnthropicError as exc:  # e.g. response validation errors
                last_error = f"anthropic error: {type(exc).__name__}"
            except Exception as exc:  # last resort: score() must never raise
                logger.exception("unexpected error calling Claude for post %s", post.id)
                last_error = f"unexpected error: {type(exc).__name__}"

            delay = min(self.retry_base_delay * (2**attempt), self.retry_max_delay)
            if status_exc is not None:
                header = _should_retry_header(status_exc)
                if header is not None:
                    retryable = header
                retry_after = _retry_after_seconds(status_exc)
                if retryable and retry_after is not None:
                    if retry_after > self.retry_max_delay:
                        retryable = False
                        last_error += f" (retry-after {retry_after:g}s)"
                    else:
                        delay = max(delay, retry_after)
            if not retryable or attempt + 1 >= attempts:
                return self._fallback(post, rules, account, last_error, started)
            logger.info(
                "claude attempt %d/%d for post %s failed (%s); retrying in %.2fs",
                attempt + 1,
                attempts,
                post.id,
                last_error,
                delay,
            )
            if delay > 0:
                await asyncio.sleep(delay)

        if response is None:  # pragma: no cover - the loop always breaks or returns
            return self._fallback(post, rules, account, last_error, started)
        return self._score_from_response(response, post, rules, account, my_tickers, started)

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
        fb = fallback_score(rules, account, error)
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
    ) -> Score:
        sc = fallback_score(rules, account, None, source="offline")
        sc.latency_ms = 0
        return sc

    async def aclose(self) -> None:
        return None
