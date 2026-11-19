"""Thin async client for the X API v2 REST endpoints ceoscan uses.

One shared ``aiohttp.ClientSession`` is passed in; this module never creates sessions.
Every non-2xx response is turned into a typed :class:`XApiError` subclass by
:func:`error_from_response`, so callers can react to credits/rate-limit/auth problems
without parsing bodies themselves.

Security: the bearer token lives only in the request headers built by
:meth:`XClient.headers`. Nothing in this module logs headers, tokens or full URLs, and
error messages are built only from X's problem ``title``/``detail`` and the status code.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from http import HTTPStatus
from typing import TYPE_CHECKING, Any

import aiohttp

from .. import USER_AGENT
from ..config import XApiSettings

if TYPE_CHECKING:  # pragma: no cover - typing only (rules.py imports this module)
    from .rules import RuleSpec

log = logging.getLogger(__name__)

REQUEST_TIMEOUT_SECONDS = 15.0
USERS_BY_BATCH = 100  # GET /2/users/by accepts at most 100 usernames per request
_MAX_DETAIL_CHARS = 500

# Endpoint path templates (also the keys of XClient.last_rate_limits).
EP_USERS = "/2/users"
EP_USERS_BY = "/2/users/by"
EP_USER_TWEETS = "/2/users/:id/tweets"
EP_STREAM_RULES = "/2/tweets/search/stream/rules"
EP_USAGE = "/2/usage/tweets"
EP_STREAM_CONNECTIONS = "/2/connections/filtered_stream"


# --------------------------------------------------------------------------- rate limits


def _header(headers: Any, name: str) -> str | None:
    """Case-insensitive header lookup that works for dicts, CIMultiDicts and None."""
    if not headers:
        return None
    try:
        value = headers.get(name)
    except AttributeError:
        return None
    if value is None and isinstance(headers, Mapping):
        lname = name.lower()
        for k, v in headers.items():
            if isinstance(k, str) and k.lower() == lname:
                value = v
                break
    if value is None:
        return None
    return str(value).strip() or None


def _int_or_none(value: str | None) -> int | None:
    if value is None:
        return None
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return None


@dataclass
class RateLimitInfo:
    """X rate-limit headers: x-rate-limit-limit / -remaining / -reset (unix seconds)."""

    limit: int | None
    remaining: int | None
    reset_at: datetime | None

    @classmethod
    def from_headers(cls, headers: Any) -> RateLimitInfo:
        limit = _int_or_none(_header(headers, "x-rate-limit-limit"))
        remaining = _int_or_none(_header(headers, "x-rate-limit-remaining"))
        reset_raw = _int_or_none(_header(headers, "x-rate-limit-reset"))
        reset_at: datetime | None = None
        if reset_raw is not None and reset_raw > 0:
            try:
                reset_at = datetime.fromtimestamp(reset_raw, tz=UTC)
            except (OverflowError, OSError, ValueError):
                reset_at = None
        return cls(limit=limit, remaining=remaining, reset_at=reset_at)

    @property
    def is_empty(self) -> bool:
        return self.limit is None and self.remaining is None and self.reset_at is None


# --------------------------------------------------------------------------- errors


class XApiError(Exception):
    """A non-2xx (or unusable) response from the X API.

    ``str(err)`` is ``"HTTP <status> <title>: <detail>"`` and never contains secrets.
    """

    def __init__(
        self,
        status: int | None = None,
        title: str | None = None,
        detail: str | None = None,
        problem_type: str | None = None,
        connection_issue: str | None = None,
        rate_limit: RateLimitInfo | None = None,
        body: dict[str, Any] | None = None,
    ) -> None:
        self.status = status
        self.title = title
        self.detail = detail
        self.problem_type = problem_type
        self.connection_issue = connection_issue
        self.rate_limit = rate_limit
        # Parsed X problem body (X problem documents carry no credentials). Not part of str().
        self.body = body
        super().__init__(self.__str__())

    def __str__(self) -> str:
        status = self.status if self.status is not None else "?"
        title = self.title or "error"
        if self.detail:
            return f"HTTP {status} {title}: {self.detail}"
        return f"HTTP {status} {title}"

    def __repr__(self) -> str:
        return f"{type(self).__name__}({self.__str__()!r})"


class CreditsDepleted(XApiError):
    """HTTP 402: the pay-per-use account is out of credits."""


class RateLimited(XApiError):
    """HTTP 429 that is not a TooManyConnections stream problem."""


class TooManyConnections(XApiError):
    """Filtered stream already at its connection limit (connection_issue == "TooManyConnections")."""


class NotAuthorized(XApiError):
    """HTTP 401 / 403."""


def _as_text(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, str):
        return value.strip() or None
    return str(value)


def _truncate(text: str | None) -> str | None:
    if text is None:
        return None
    text = " ".join(text.split())
    if len(text) > _MAX_DETAIL_CHARS:
        text = text[: _MAX_DETAIL_CHARS - 1] + "…"
    return text


def _parse_body(body: dict | str | bytes | None) -> tuple[dict[str, Any] | None, str | None]:
    """Return (problem dict, raw text) from whatever the response body was."""
    if body is None:
        return None, None
    if isinstance(body, dict):
        return body, None
    if isinstance(body, bytes):
        body = body.decode("utf-8", errors="replace")
    text = str(body).strip()
    if not text:
        return None, None
    try:
        parsed = json.loads(text)
    except ValueError:
        return None, text
    if isinstance(parsed, dict):
        return parsed, None
    return None, text


def _is_credits_problem(title: str | None, detail: str | None, problem_type: str | None) -> bool:
    t = (title or "").lower().replace(" ", "")
    d = (detail or "").lower()
    p = (problem_type or "").lower()
    return t == "creditsdepleted" or "/problems/credits" in p or "credits depleted" in d


def error_from_response(status: int, body: dict | str | None, headers: Any) -> XApiError:
    """Build the most specific :class:`XApiError` subclass for an X error response.

    Precedence: CreditsDepleted (402 or an X "credits" problem) > TooManyConnections
    (connection_issue) > RateLimited (429) > NotAuthorized (401/403) > XApiError.
    """
    problem, raw_text = _parse_body(body)
    title = detail = problem_type = connection_issue = None
    if problem is not None:
        title = _as_text(problem.get("title"))
        detail = _as_text(problem.get("detail"))
        problem_type = _as_text(problem.get("type"))
        connection_issue = _as_text(problem.get("connection_issue"))
        errors = problem.get("errors")
        if isinstance(errors, list) and errors and isinstance(errors[0], dict):
            first = errors[0]
            title = title or _as_text(first.get("title"))
            detail = detail or _as_text(first.get("detail")) or _as_text(first.get("message"))
            problem_type = problem_type or _as_text(first.get("type"))
            if connection_issue is None:
                for err in errors:
                    if isinstance(err, dict) and err.get("connection_issue"):
                        connection_issue = _as_text(err.get("connection_issue"))
                        break
        if detail is None:
            detail = _as_text(problem.get("reason"))
    elif raw_text:
        detail = raw_text
    if title is None:
        try:
            title = HTTPStatus(status).phrase
        except ValueError:
            title = None
    detail = _truncate(detail)
    rate_limit = RateLimitInfo.from_headers(headers) if headers is not None else None

    cls: type[XApiError]
    if status == 402 or _is_credits_problem(title, detail, problem_type):
        cls = CreditsDepleted
    elif connection_issue == "TooManyConnections":
        cls = TooManyConnections
    elif status == 429:
        cls = RateLimited
    elif status in (401, 403):
        cls = NotAuthorized
    else:
        cls = XApiError
    return cls(
        status=status,
        title=title,
        detail=detail,
        problem_type=problem_type,
        connection_issue=connection_issue,
        rate_limit=rate_limit,
        body=problem,
    )


# --------------------------------------------------------------------------- client


def _csv(values: list[str] | tuple[str, ...]) -> str:
    return ",".join(dict.fromkeys(v.strip() for v in values if v and v.strip()))


def _has_rate_limit_headers(headers: Any) -> bool:
    return any(
        _header(headers, h) is not None for h in ("x-rate-limit-limit", "x-rate-limit-remaining", "x-rate-limit-reset")
    )


class XClient:
    """Async X API v2 client (bearer-token / app-only auth)."""

    def __init__(self, session: aiohttp.ClientSession, bearer_token: str, settings: XApiSettings) -> None:
        if not bearer_token:
            raise ValueError("an X bearer token is required (set X_BEARER_TOKEN)")
        self._session = session
        self._token = bearer_token
        self.settings = settings
        self.base_url = settings.base_url.rstrip("/")
        self.timeout = aiohttp.ClientTimeout(total=REQUEST_TIMEOUT_SECONDS)
        self.last_rate_limits: dict[str, RateLimitInfo] = {}

    def __repr__(self) -> str:  # never show the token
        return f"XClient(base_url={self.base_url!r})"

    def headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self._token}", "User-Agent": USER_AGENT}

    async def _request(
        self,
        method: str,
        path: str,
        template: str,
        *,
        params: dict[str, str] | None = None,
        json_body: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Send one request; return the parsed JSON object or raise XApiError.

        Network errors (aiohttp.ClientError, asyncio.TimeoutError) propagate unchanged.
        """
        url = f"{self.base_url}{path}"
        async with self._session.request(
            method,
            url,
            params=params or None,
            json=json_body,
            headers=self.headers(),
            timeout=self.timeout,
        ) as resp:
            status = resp.status
            if _has_rate_limit_headers(resp.headers):
                self.last_rate_limits[template] = RateLimitInfo.from_headers(resp.headers)
            raw = await resp.read()
            log.debug("X %s %s -> %s", method, template, status)
            if not 200 <= status < 300:
                raise error_from_response(status, raw.decode("utf-8", errors="replace"), resp.headers)
        if not raw.strip():
            return {}
        try:
            data = json.loads(raw)
        except ValueError:
            raise XApiError(status=status, title="Invalid response", detail="response body is not JSON") from None
        if not isinstance(data, dict):
            raise XApiError(status=status, title="Invalid response", detail="response body is not a JSON object")
        return data

    # ------------------------------------------------------------------ users

    async def lookup_users(self, usernames: list[str], user_fields: list[str]) -> tuple[list[dict], list[dict]]:
        """GET /2/users/by in batches of 100. Returns (users, errors).

        Not-found / suspended handles come back in ``errors`` (each with ``"value": handle``).
        """
        unique: list[str] = []
        seen: set[str] = set()
        for raw_name in usernames:
            name = (raw_name or "").strip().lstrip("@")
            if name and name.lower() not in seen:  # each user read is billed: never ask twice
                seen.add(name.lower())
                unique.append(name)
        users: list[dict] = []
        errors: list[dict] = []
        fields = _csv(user_fields)
        for i in range(0, len(unique), USERS_BY_BATCH):
            batch = unique[i : i + USERS_BY_BATCH]
            params = {"usernames": ",".join(batch)}
            if fields:
                params["user.fields"] = fields
            body = await self._request("GET", EP_USERS_BY, EP_USERS_BY, params=params)
            users.extend(d for d in body.get("data") or [] if isinstance(d, dict))
            errors.extend(e for e in body.get("errors") or [] if isinstance(e, dict))
        return users, errors

    async def lookup_users_by_ids(self, ids: list[str], user_fields: list[str]) -> tuple[list[dict], list[dict]]:
        """GET /2/users?ids=... in batches of 100. Returns (users, errors).

        Used by ``verify`` to follow a pinned user_id whose handle no longer resolves (renamed account).
        Ids that are gone or suspended come back in ``errors``. Non-numeric ids are skipped.
        """
        unique = list(dict.fromkeys(str(i).strip() for i in ids if str(i).strip().isdigit()))
        users: list[dict] = []
        errors: list[dict] = []
        fields = _csv(user_fields)
        for i in range(0, len(unique), USERS_BY_BATCH):
            params = {"ids": ",".join(unique[i : i + USERS_BY_BATCH])}
            if fields:
                params["user.fields"] = fields
            body = await self._request("GET", EP_USERS, EP_USERS, params=params)
            users.extend(d for d in body.get("data") or [] if isinstance(d, dict))
            errors.extend(e for e in body.get("errors") or [] if isinstance(e, dict))
        return users, errors

    # ------------------------------------------------------------------ stream rules

    async def get_rules(self) -> list[dict]:
        """All filtered-stream rules on this app: [{"id", "value", "tag"?}, ...]."""
        rules: list[dict] = []
        params: dict[str, str] = {}
        for _ in range(100):  # hard stop; 1,000 rules fit on one page
            body = await self._request("GET", EP_STREAM_RULES, EP_STREAM_RULES, params=params)
            rules.extend(r for r in body.get("data") or [] if isinstance(r, dict))
            next_token = (body.get("meta") or {}).get("next_token")
            if not next_token:
                break
            params = {"pagination_token": str(next_token)}
        return rules

    async def add_rules(self, rules: list[RuleSpec], dry_run: bool = False) -> dict:
        """POST {"add": [...]} to the rules endpoint. Returns X's full JSON response.

        Invalid rules are reported by X in a 200 response's ``errors`` list (not as an
        exception); check it. An empty list makes no request.
        """
        if not rules:
            return _empty_rules_response()
        payload = {"add": [{"value": r.value, "tag": r.tag} if r.tag else {"value": r.value} for r in rules]}
        params = {"dry_run": "true"} if dry_run else None
        return await self._request("POST", EP_STREAM_RULES, EP_STREAM_RULES, params=params, json_body=payload)

    async def delete_rules(self, ids: list[str], dry_run: bool = False) -> dict:
        """POST {"delete": {"ids": [...]}}. Returns X's full JSON response. Empty list -> no request."""
        ids = [str(i) for i in ids if i]
        if not ids:
            return _empty_rules_response()
        payload = {"delete": {"ids": ids}}
        params = {"dry_run": "true"} if dry_run else None
        return await self._request("POST", EP_STREAM_RULES, EP_STREAM_RULES, params=params, json_body=payload)

    # ------------------------------------------------------------------ usage / connections

    async def get_usage(self) -> dict:
        """GET /2/usage/tweets -> the "data" object (project_usage, project_cap, cap_reset_day ...)."""
        body = await self._request("GET", EP_USAGE, EP_USAGE)
        data = body.get("data")
        return data if isinstance(data, dict) else {}

    async def terminate_stream_connections(self) -> bool:
        """DELETE /2/connections/filtered_stream: end this app's (stale) stream connections.

        Returns True on success, False on any API or network failure (logged, no secrets).
        """
        try:
            await self._request("DELETE", EP_STREAM_CONNECTIONS, EP_STREAM_CONNECTIONS)
        except XApiError as exc:
            log.warning("could not terminate stale stream connections: %s", exc)
            return False
        except (TimeoutError, aiohttp.ClientError) as exc:
            log.warning("could not terminate stale stream connections: %s", type(exc).__name__)
            return False
        log.info("terminated stale filtered-stream connections")
        return True

    # ------------------------------------------------------------------ timelines

    async def get_user_posts(
        self,
        user_id: str,
        *,
        since_id: str | None,
        max_results: int,
        exclude: list[str],
        tweet_fields: list[str],
        expansions: list[str],
        user_fields: list[str],
        pagination_token: str | None = None,
    ) -> dict:
        """GET /2/users/:id/tweets. Returns the full JSON (data newest-first, includes, meta).

        ``meta.next_token`` is set when more posts match than ``max_results``; pass it back as
        ``pagination_token`` (with the same ``since_id``) to get the next, older page.
        Updates ``last_rate_limits["/2/users/:id/tweets"]``.
        """
        uid = str(user_id).strip()
        if not uid.isdigit():
            raise ValueError("user_id must be a numeric X user id")
        params: dict[str, str] = {"max_results": str(max(5, min(100, int(max_results))))}
        if since_id:
            params["since_id"] = str(since_id)
        if pagination_token:
            params["pagination_token"] = str(pagination_token)
        for key, values in (
            ("exclude", exclude),
            ("tweet.fields", tweet_fields),
            ("expansions", expansions),
            ("user.fields", user_fields),
        ):
            joined = _csv(values)
            if joined:
                params[key] = joined
        return await self._request("GET", f"/2/users/{uid}/tweets", EP_USER_TWEETS, params=params)


def _empty_rules_response() -> dict:
    return {"data": [], "meta": {"summary": {"created": 0, "not_created": 0, "valid": 0, "invalid": 0}}}
