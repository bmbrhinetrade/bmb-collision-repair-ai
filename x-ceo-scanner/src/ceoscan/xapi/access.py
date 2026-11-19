"""Startup access check: which ingest mode can this X app use, and how fast will it be?

``check_access`` asks X three cheap questions:

1. ``GET /2/tweets/search/stream/rules`` - 200 means the stream endpoints are allowed.
2. ``GET /2/usage/tweets``              - monthly post-read usage vs cap.
3. a stream connect probe              - status code only, closed immediately:
   200 ok, 402 credits depleted, 403 no stream access, 429/409 busy.

None of these return posts, so the check costs nothing.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

import aiohttp

from ..config import XApiSettings
from .client import NotAuthorized, XApiError, XClient, error_from_response
from .poller import compute_poll_interval, estimate_poll_latency
from .stream import indicates_no_stream_access, read_error_body, stream_headers, stream_url

log = logging.getLogger(__name__)

#: X documents ~4-5 s P99 delivery for the filtered stream; plus scoring and the push.
STREAM_LATENCY_S: tuple[float, float] = (4.5, 8.0)
PROBE_TIMEOUT_SECONDS = 10.0


@dataclass
class AccessReport:
    mode: str  # "stream" | "poll"
    stream_available: bool | None
    credits_ok: bool | None
    usage: dict | None
    existing_rules: int | None
    poll_interval_s: float | None
    est_latency_s: tuple[float, float]
    messages: list[str] = field(default_factory=list)
    # False when X answered any check with HTTP 401 (bad / revoked bearer token), True when a check got an
    # authenticated answer, None when nothing answered (network problem)
    auth_ok: bool | None = None


@dataclass
class _Probe:
    status: int | None
    error: XApiError | None = None
    network_error: str | None = None


def _describe_exc(exc: BaseException) -> str:
    text = " ".join(str(exc).split())
    return f"{type(exc).__name__}: {text}"[:300] if text else type(exc).__name__


async def probe_stream(
    session: aiohttp.ClientSession,
    bearer_token: str,
    settings: XApiSettings,
    timeout: float = PROBE_TIMEOUT_SECONDS,
) -> _Probe:
    """Open the filtered stream, look at the status code, close it straight away.

    No query parameters are sent (in particular no backfill), and the body of a 200 is
    never read, so no posts are delivered or billed.
    """
    client_timeout = aiohttp.ClientTimeout(
        total=timeout, sock_connect=min(timeout, settings.stream.connect_timeout_seconds)
    )
    try:
        async with session.get(
            stream_url(settings), headers=stream_headers(bearer_token), timeout=client_timeout
        ) as resp:
            if resp.status == 200:
                resp.close()  # drop the connection without reading the stream
                return _Probe(200)
            body = await read_error_body(resp)
            return _Probe(resp.status, error=error_from_response(resp.status, body, resp.headers))
    except (aiohttp.ClientError, OSError, TimeoutError) as exc:
        return _Probe(None, network_error=_describe_exc(exc))


def _as_int(value: Any) -> int | None:
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return None


async def check_access(
    client: XClient,
    settings: XApiSettings,
    n_accounts: int,
    session: aiohttp.ClientSession,
    bearer_token: str,
) -> AccessReport:
    messages: list[str] = []
    rules_allowed: bool | None = None
    existing_rules: int | None = None
    credits_ok: bool | None = None
    usage: dict | None = None
    auth_ok: bool | None = None

    # 1. stream rules endpoint
    try:
        rules = await client.get_rules()
        rules_allowed = auth_ok = True
        existing_rules = len(rules)
        messages.append(f"stream rules endpoint OK ({existing_rules} existing rules)")
    except NotAuthorized as exc:
        if exc.status == 403:
            rules_allowed = False
            auth_ok = True if auth_ok is None else auth_ok
            messages.append(f"stream rules endpoint forbidden ({exc})")
        else:
            auth_ok = False
            messages.append(f"X rejected the bearer token ({exc}); check X_BEARER_TOKEN")
    except XApiError as exc:
        if exc.status == 402:
            credits_ok = False
        messages.append(f"stream rules check failed: {exc}")
    except (aiohttp.ClientError, OSError, TimeoutError) as exc:
        messages.append(f"stream rules check failed: {_describe_exc(exc)}")

    # 2. usage
    try:
        usage = await client.get_usage()
        auth_ok = True if auth_ok is None else auth_ok
        used = _as_int(usage.get("project_usage"))
        cap = _as_int(usage.get("project_cap"))
        if used is not None and cap:
            line = f"usage: {used:,} / {cap:,} post reads this billing cycle"
            if usage.get("cap_reset_day") is not None:
                line += f" (resets on day {usage['cap_reset_day']})"
            messages.append(line)
            if used >= cap:
                messages.append("monthly post cap reached - posts will not be delivered until it resets")
    except XApiError as exc:
        if exc.status == 402:
            credits_ok = False
        elif exc.status == 401:
            auth_ok = False
        messages.append(f"usage check failed: {exc}")
    except (aiohttp.ClientError, OSError, TimeoutError) as exc:
        messages.append(f"usage check failed: {_describe_exc(exc)}")

    # 3. stream probe
    probe = await probe_stream(session, bearer_token, settings)
    stream_available: bool | None = None
    if probe.status == 200:
        stream_available, credits_ok = True, True
        messages.append("filtered stream: connected OK (probe closed immediately)")
    elif probe.status == 402:
        stream_available, credits_ok = True, False
        messages.append(
            "filtered stream: X API credits depleted (HTTP 402) - add credits at console.x.com; "
            "the stream will connect once credits are available"
        )
    elif probe.status == 403:
        stream_available = False
        why = "not available on this plan" if probe.error and indicates_no_stream_access(probe.error) else "forbidden"
        messages.append(f"filtered stream: {why} ({probe.error})")
    elif probe.status == 409 and probe.error is not None and "must define rules" in str(probe.error).lower():
        # X answers a connect with no rules on the app with 409 - only after billing and access passed.
        stream_available, credits_ok = True, True
        messages.append(
            "filtered stream: available (X wants at least one rule before it connects; "
            "`rules --apply` or `run` adds them)"
        )
    elif probe.status in (409, 429):
        stream_available = True
        messages.append(
            f"filtered stream: busy ({probe.error}) - another connection may be open or the "
            "connect limit (50 per 15 min) was hit; it will be retried"
        )
    elif probe.status == 401:
        auth_ok = False
        messages.append(f"filtered stream: X rejected the bearer token ({probe.error})")
    elif probe.status is not None:
        messages.append(f"filtered stream probe: {probe.error}")
    else:
        messages.append(f"filtered stream probe failed: {probe.network_error}")
    if auth_ok is None and probe.status is not None and probe.status != 401:
        auth_ok = True  # any other HTTP answer to the probe means X accepted the token
    if stream_available is None and rules_allowed is not None:
        stream_available = rules_allowed

    # mode + latency
    requested = settings.mode
    if requested in ("stream", "poll"):
        mode = requested
        if mode == "stream" and stream_available is False:
            messages.append("mode forced to stream, but the stream looks unavailable for this app")
    else:
        mode = "poll" if stream_available is False else "stream"
    poll_interval = compute_poll_interval(n_accounts, settings.poll) if n_accounts > 0 else None
    if mode == "stream":
        est_latency = STREAM_LATENCY_S
    else:
        est_latency = estimate_poll_latency(
            poll_interval if poll_interval is not None else settings.poll.min_interval_seconds
        )
    if mode == "poll" and requested == "auto":
        messages.append(
            f"falling back to timeline polling: each account every {poll_interval or 0:.1f}s "
            f"(est. latency {est_latency[0]:.1f}-{est_latency[1]:.1f}s)"
        )

    report = AccessReport(
        mode=mode,
        stream_available=stream_available,
        credits_ok=credits_ok,
        usage=usage,
        existing_rules=existing_rules,
        poll_interval_s=poll_interval,
        est_latency_s=est_latency,
        messages=messages,
        auth_ok=auth_ok,
    )
    log.debug("access check: mode=%s stream=%s credits=%s", mode, stream_available, credits_ok)
    return report


def _yes_no(value: bool | None, yes: str, no: str) -> str:
    if value is None:
        return "unknown"
    return yes if value else no


def format_access_report(r: AccessReport) -> str:
    lines = ["X API access check", f"  mode:             {r.mode}"]
    lines.append(f"  bearer token:     {_yes_no(r.auth_ok, 'accepted', 'REJECTED (HTTP 401) - check X_BEARER_TOKEN')}")
    lines.append(f"  filtered stream:  {_yes_no(r.stream_available, 'available', 'NOT available')}")
    lines.append(f"  credits:          {_yes_no(r.credits_ok, 'ok', 'DEPLETED - add credits at console.x.com')}")
    if r.usage:
        used = _as_int(r.usage.get("project_usage"))
        cap = _as_int(r.usage.get("project_cap"))
        if used is not None and cap:
            lines.append(f"  usage:            {used:,} / {cap:,} post reads ({used / cap:.1%})")
    if r.existing_rules is not None:
        lines.append(f"  stream rules:     {r.existing_rules} on X")
    if r.poll_interval_s is not None:
        label = "per account" if r.mode == "poll" else "per account if polling"
        lines.append(f"  poll interval:    {r.poll_interval_s:.1f}s {label}")
    lines.append(f"  est. latency:     typical {r.est_latency_s[0]:.1f}s, worst {r.est_latency_s[1]:.1f}s")
    if r.messages:
        lines.append("  notes:")
        lines.extend(f"    - {m}" for m in r.messages)
    return "\n".join(lines)
