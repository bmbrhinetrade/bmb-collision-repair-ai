"""Unit tests for the X API v2 client (xapi/client.py). All HTTP is mocked with aioresponses."""

from __future__ import annotations

import json
import logging
import re
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any

import aiohttp
import aioresponses.core as aioresponses_core
import pytest
from aioresponses import CallbackResult, aioresponses
from multidict import CIMultiDict

from ceoscan import USER_AGENT
from ceoscan.config import XApiSettings
from ceoscan.xapi.client import (
    CreditsDepleted,
    NotAuthorized,
    RateLimited,
    RateLimitInfo,
    TooManyConnections,
    XApiError,
    XClient,
    error_from_response,
)
from ceoscan.xapi.rules import RuleSpec

TOKEN = "AAAAtest-bearer-token-value-123456"
USERS_BY = re.compile(r"^https://api\.x\.com/2/users/by\?.*$")
RULES = re.compile(r"^https://api\.x\.com/2/tweets/search/stream/rules(\?.*)?$")
USAGE = "https://api.x.com/2/usage/tweets"
CONNECTIONS = "https://api.x.com/2/connections/filtered_stream"
TIMELINE = re.compile(r"^https://api\.x\.com/2/users/44196397/tweets\?.*$")

CREDITS_BODY_1 = {
    "title": "CreditsDepleted",
    "detail": "Your enrolled account does not have any credits to fulfill this request.",
    "type": "https://api.twitter.com/2/problems/credits",
}
CREDITS_BODY_2 = {
    "detail": "credits depleted",
    "status": 402,
    "title": "Payment Required",
    "type": "https://api.x.com/2/problems/credits-depleted",
}
TOO_MANY_BODY = {
    "title": "ConnectionException",
    "detail": "This stream is currently at the maximum allowed connection limit.",
    "connection_issue": "TooManyConnections",
    "type": "https://api.twitter.com/2/problems/streaming-connection",
}


class _CompatClientResponse(aiohttp.ClientResponse):
    """aioresponses 0.7.9 does not pass ``stream_writer`` (required by the pinned aiohttp 3.14)."""

    def __init__(self, method: str, url: Any, **kwargs: Any) -> None:
        kwargs.setdefault("stream_writer", SimpleNamespace(output_size=0))
        super().__init__(method, url, **kwargs)


@pytest.fixture(autouse=True)
def _aioresponses_compat(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(aioresponses_core, "ClientResponse", _CompatClientResponse)


@pytest.fixture
async def client():
    async with aiohttp.ClientSession() as session:
        yield XClient(session, TOKEN, XApiSettings())


class Calls:
    """Callback that records (url, kwargs) and returns queued or computed responses."""

    def __init__(self, respond) -> None:
        self.respond = respond
        self.seen: list[tuple[Any, dict]] = []

    def __call__(self, url, **kwargs) -> CallbackResult:
        self.seen.append((url, kwargs))
        return self.respond(url, **kwargs)


# --------------------------------------------------------------------------- RateLimitInfo


def test_rate_limit_info_from_headers_dict_any_case() -> None:
    info = RateLimitInfo.from_headers(
        {"X-Rate-Limit-Limit": "300", "x-rate-limit-remaining": "299", "X-RATE-LIMIT-RESET": "1795000000"}
    )
    assert info.limit == 300
    assert info.remaining == 299
    assert info.reset_at == datetime.fromtimestamp(1795000000, tz=UTC)
    assert info.reset_at.tzinfo is not None


def test_rate_limit_info_from_multidict() -> None:
    headers = CIMultiDict(
        {"x-rate-limit-limit": "450", "x-rate-limit-remaining": "0", "x-rate-limit-reset": "1795000123"}
    )
    info = RateLimitInfo.from_headers(headers)
    assert (info.limit, info.remaining) == (450, 0)
    assert info.reset_at == datetime.fromtimestamp(1795000123, tz=UTC)


def test_rate_limit_info_missing_or_garbage_headers() -> None:
    assert RateLimitInfo.from_headers(None) == RateLimitInfo(None, None, None)
    assert RateLimitInfo.from_headers({}) == RateLimitInfo(None, None, None)
    info = RateLimitInfo.from_headers(
        {"x-rate-limit-limit": "abc", "x-rate-limit-remaining": "", "x-rate-limit-reset": "-5"}
    )
    assert info == RateLimitInfo(None, None, None)
    partial = RateLimitInfo.from_headers({"x-rate-limit-remaining": "7"})
    assert partial == RateLimitInfo(None, 7, None)


# --------------------------------------------------------------------------- error_from_response


@pytest.mark.parametrize("body", [CREDITS_BODY_1, CREDITS_BODY_2])
def test_402_credits_depleted_both_body_shapes(body: dict) -> None:
    err = error_from_response(402, body, {})
    assert type(err) is CreditsDepleted
    assert err.status == 402
    assert err.title == body["title"]
    assert err.detail == body["detail"]
    assert err.problem_type == body["type"]
    assert str(err) == f"HTTP 402 {body['title']}: {body['detail']}"


@pytest.mark.parametrize("body", [CREDITS_BODY_1, CREDITS_BODY_2])
def test_402_credits_depleted_from_json_text_body(body: dict) -> None:
    err = error_from_response(402, json.dumps(body), None)
    assert isinstance(err, CreditsDepleted)
    assert err.rate_limit is None


def test_credits_problem_type_wins_even_with_other_status() -> None:
    assert isinstance(error_from_response(403, CREDITS_BODY_1, {}), CreditsDepleted)
    assert isinstance(error_from_response(429, CREDITS_BODY_2, {}), CreditsDepleted)


def test_402_without_body_is_still_credits_depleted() -> None:
    err = error_from_response(402, None, {})
    assert isinstance(err, CreditsDepleted)
    assert str(err) == "HTTP 402 Payment Required"


def test_429_is_rate_limited_with_reset() -> None:
    headers = {"x-rate-limit-limit": "50", "x-rate-limit-remaining": "0", "x-rate-limit-reset": "1795000000"}
    err = error_from_response(
        429,
        {"title": "Too Many Requests", "detail": "Too Many Requests", "type": "about:blank", "status": 429},
        headers,
    )
    assert type(err) is RateLimited
    assert not isinstance(err, TooManyConnections)
    assert err.rate_limit is not None
    assert err.rate_limit.remaining == 0
    assert err.rate_limit.reset_at == datetime.fromtimestamp(1795000000, tz=UTC)


@pytest.mark.parametrize("status", [429, 409])
def test_too_many_connections_vs_rate_limited(status: int) -> None:
    err = error_from_response(status, TOO_MANY_BODY, {})
    assert type(err) is TooManyConnections
    assert not isinstance(err, RateLimited)
    assert err.connection_issue == "TooManyConnections"
    assert "maximum allowed connection limit" in str(err)


def test_too_many_connections_inside_errors_array() -> None:
    body = {"errors": [{"title": "ConnectionException", "connection_issue": "TooManyConnections", "detail": "limit"}]}
    err = error_from_response(429, body, {})
    assert isinstance(err, TooManyConnections)
    assert err.title == "ConnectionException"


@pytest.mark.parametrize("status", [401, 403])
def test_not_authorized(status: int) -> None:
    body = {
        "title": "Unauthorized" if status == 401 else "Forbidden",
        "detail": "nope",
        "type": "about:blank",
        "status": status,
    }
    err = error_from_response(status, body, {})
    assert type(err) is NotAuthorized
    assert err.status == status


def test_403_client_not_enrolled_keeps_type_and_body() -> None:
    body = {
        "title": "Client Forbidden",
        "detail": "When authenticating requests to the Twitter API v2 endpoints, you must use keys and tokens "
        "from a Twitter developer App that is attached to a Project.",
        "reason": "client-not-enrolled",
        "type": "https://api.twitter.com/2/problems/client-forbidden",
        "required_enrollment": "Appropriate Level of API Access",
    }
    err = error_from_response(403, body, {})
    assert isinstance(err, NotAuthorized)
    assert err.problem_type == "https://api.twitter.com/2/problems/client-forbidden"
    assert err.body is not None and err.body["reason"] == "client-not-enrolled"


def test_other_statuses_are_plain_xapierror() -> None:
    err = error_from_response(503, "<html>Service Unavailable</html>", {})
    assert type(err) is XApiError
    assert err.title == "Service Unavailable"
    assert err.detail == "<html>Service Unavailable</html>"
    assert str(err) == "HTTP 503 Service Unavailable: <html>Service Unavailable</html>"
    legacy = error_from_response(400, {"errors": [{"message": "Invalid Request", "code": 214}]}, {})
    assert type(legacy) is XApiError
    assert legacy.detail == "Invalid Request"
    assert issubclass(CreditsDepleted, XApiError) and issubclass(NotAuthorized, XApiError)


def test_long_detail_is_truncated() -> None:
    err = error_from_response(500, {"title": "Internal", "detail": "x" * 5000}, {})
    assert len(err.detail or "") <= 500


# --------------------------------------------------------------------------- headers


async def test_headers_include_authorization_and_user_agent(client: XClient) -> None:
    headers = client.headers()
    assert headers["Authorization"] == f"Bearer {TOKEN}"
    assert headers["User-Agent"] == USER_AGENT
    assert TOKEN not in repr(client)


async def test_requests_send_authorization_and_user_agent(client: XClient) -> None:
    calls = Calls(
        lambda url, **kw: CallbackResult(status=200, payload={"data": {"project_usage": "12", "cap_reset_day": 19}})
    )
    with aioresponses() as m:
        m.get(USAGE, callback=calls)
        usage = await client.get_usage()
    assert usage == {"project_usage": "12", "cap_reset_day": 19}
    sent = calls.seen[0][1]["headers"]
    assert sent["Authorization"] == f"Bearer {TOKEN}"
    assert sent["User-Agent"] == USER_AGENT
    timeout = calls.seen[0][1]["timeout"]
    assert isinstance(timeout, aiohttp.ClientTimeout) and timeout.total == 15


# --------------------------------------------------------------------------- lookup_users


async def test_lookup_users_batches_by_100(client: XClient) -> None:
    def respond(url, **kwargs):
        names = url.query["usernames"].split(",")
        data = [{"id": str(1000 + int(n[1:])), "username": n} for n in names if not n.endswith("7")]
        errors = [
            {"value": n, "detail": f"Could not find user with usernames: [{n}].", "title": "Not Found Error"}
            for n in names
            if n.endswith("7")
        ]
        return CallbackResult(status=200, payload={"data": data, "errors": errors})

    calls = Calls(respond)
    handles = [f"u{i}" for i in range(250)]
    with aioresponses() as m:
        m.get(USERS_BY, callback=calls, repeat=True)
        users, errors = await client.lookup_users(handles + ["@U0", "u1"], ["verified", "public_metrics"])

    batches = [url.query["usernames"].split(",") for url, _ in calls.seen]
    assert [len(b) for b in batches] == [100, 100, 50]  # duplicates (any case, "@") are not re-requested
    assert [n for b in batches for n in b] == handles
    assert all(url.query["user.fields"] == "verified,public_metrics" for url, _ in calls.seen)
    assert len(users) + len(errors) == 250
    assert {e["value"] for e in errors} == {h for h in handles if h.endswith("7")}
    assert users[0] == {"id": "1000", "username": "u0"}


async def test_lookup_users_empty_makes_no_request(client: XClient) -> None:
    with aioresponses():  # any request would fail with "Connection refused"
        assert await client.lookup_users([], ["verified"]) == ([], [])


async def test_lookup_users_records_rate_limit_and_raises_typed_errors(client: XClient) -> None:
    headers = {"x-rate-limit-limit": "300", "x-rate-limit-remaining": "0", "x-rate-limit-reset": "1795000000"}
    with aioresponses() as m:
        m.get(
            USERS_BY, status=429, payload={"title": "Too Many Requests", "detail": "Too Many Requests"}, headers=headers
        )
        with pytest.raises(RateLimited) as exc_info:
            await client.lookup_users(["elonmusk"], ["verified"])
    assert exc_info.value.rate_limit is not None and exc_info.value.rate_limit.remaining == 0
    assert client.last_rate_limits["/2/users/by"].limit == 300


# --------------------------------------------------------------------------- rules endpoints


async def test_get_rules_follows_pagination(client: XClient) -> None:
    def respond(url, **kwargs):
        if "pagination_token" not in url.query:
            return CallbackResult(
                status=200, payload={"data": [{"id": "1", "value": "a", "tag": "t"}], "meta": {"next_token": "abc"}}
            )
        assert url.query["pagination_token"] == "abc"
        return CallbackResult(status=200, payload={"data": [{"id": "2", "value": "b"}], "meta": {"result_count": 1}})

    with aioresponses() as m:
        m.get(RULES, callback=Calls(respond), repeat=True)
        rules = await client.get_rules()
    assert [r["id"] for r in rules] == ["1", "2"]


async def test_get_rules_empty(client: XClient) -> None:
    with aioresponses() as m:
        m.get(RULES, payload={"meta": {"sent": "2026-11-19T00:00:00.000Z", "result_count": 0}})
        assert await client.get_rules() == []


async def test_add_rules_payload_and_dry_run(client: XClient) -> None:
    calls = Calls(
        lambda url, **kw: CallbackResult(status=200, payload={"meta": {"summary": {"valid": 1, "invalid": 0}}})
    )
    specs = [RuleSpec("from:1 -is:retweet", "ceoscan:acct:r1t0:1")]
    with aioresponses() as m:
        m.post(RULES, callback=calls, repeat=True)
        resp = await client.add_rules(specs, dry_run=True)
        await client.add_rules(specs)
    assert resp["meta"]["summary"]["valid"] == 1
    (url1, kw1), (url2, kw2) = calls.seen
    assert url1.query.get("dry_run") == "true"
    assert "dry_run" not in url2.query
    assert kw1["json"] == {"add": [{"value": "from:1 -is:retweet", "tag": "ceoscan:acct:r1t0:1"}]}
    assert kw2["json"] == kw1["json"]


async def test_delete_rules_payload(client: XClient) -> None:
    calls = Calls(
        lambda url, **kw: CallbackResult(status=200, payload={"meta": {"summary": {"deleted": 2, "not_deleted": 0}}})
    )
    with aioresponses() as m:
        m.post(RULES, callback=calls)
        resp = await client.delete_rules(["10", "11"])
    assert resp["meta"]["summary"]["deleted"] == 2
    assert calls.seen[0][1]["json"] == {"delete": {"ids": ["10", "11"]}}


async def test_add_and_delete_empty_make_no_request(client: XClient) -> None:
    with aioresponses():
        assert (await client.add_rules([]))["meta"]["summary"]["created"] == 0
        assert (await client.delete_rules([]))["data"] == []


# --------------------------------------------------------------------------- usage / connections


async def test_get_usage_credits_depleted(client: XClient) -> None:
    with aioresponses() as m:
        m.get(USAGE, status=402, payload=CREDITS_BODY_2)
        with pytest.raises(CreditsDepleted) as exc_info:
            await client.get_usage()
    assert exc_info.value.status == 402
    assert TOKEN not in str(exc_info.value)


async def test_terminate_stream_connections(client: XClient, caplog: pytest.LogCaptureFixture) -> None:
    calls = Calls(lambda url, **kw: CallbackResult(status=200, payload={"data": {"killed_connections": True}}))
    with aioresponses() as m:
        m.delete(CONNECTIONS, callback=calls)
        assert await client.terminate_stream_connections() is True
    assert calls.seen[0][1]["headers"]["Authorization"] == f"Bearer {TOKEN}"

    caplog.set_level(logging.DEBUG)
    with aioresponses() as m:
        m.delete(CONNECTIONS, status=403, payload={"title": "Forbidden", "detail": "not allowed"})
        assert await client.terminate_stream_connections() is False
    with aioresponses() as m:
        m.delete(CONNECTIONS, exception=aiohttp.ClientConnectionError("boom"))
        assert await client.terminate_stream_connections() is False
    assert TOKEN not in caplog.text


# --------------------------------------------------------------------------- timelines


async def test_get_user_posts_params_and_rate_limits(client: XClient) -> None:
    body = {
        "data": [{"id": "2", "text": "b"}, {"id": "1", "text": "a"}],
        "meta": {"newest_id": "2", "oldest_id": "1", "result_count": 2},
    }
    headers = {"x-rate-limit-limit": "10000", "x-rate-limit-remaining": "9999", "x-rate-limit-reset": "1795000000"}
    calls = Calls(lambda url, **kw: CallbackResult(status=200, payload=body, headers=headers))
    with aioresponses() as m:
        m.get(TIMELINE, callback=calls)
        resp = await client.get_user_posts(
            "44196397",
            since_id="1850000000000000000",
            max_results=10,
            exclude=["retweets", "replies"],
            tweet_fields=["created_at", "note_tweet"],
            expansions=["author_id"],
            user_fields=["username"],
        )
    assert resp == body
    q = calls.seen[0][0].query
    assert q["since_id"] == "1850000000000000000"
    assert q["max_results"] == "10"
    assert q["exclude"] == "retweets,replies"
    assert q["tweet.fields"] == "created_at,note_tweet"
    assert q["expansions"] == "author_id"
    assert q["user.fields"] == "username"
    info = client.last_rate_limits["/2/users/:id/tweets"]
    assert (info.limit, info.remaining) == (10000, 9999)


async def test_get_user_posts_omits_empty_params_and_clamps(client: XClient) -> None:
    calls = Calls(lambda url, **kw: CallbackResult(status=200, payload={"meta": {"result_count": 0}}))
    with aioresponses() as m:
        m.get(TIMELINE, callback=calls)
        await client.get_user_posts(
            "44196397", since_id=None, max_results=1, exclude=[], tweet_fields=[], expansions=[], user_fields=[]
        )
    q = calls.seen[0][0].query
    assert dict(q) == {"max_results": "5"}


async def test_get_user_posts_rejects_non_numeric_id(client: XClient) -> None:
    with pytest.raises(ValueError):
        await client.get_user_posts(
            "../2/usage", since_id=None, max_results=10, exclude=[], tweet_fields=[], expansions=[], user_fields=[]
        )


async def test_get_user_posts_server_error_and_bad_json(client: XClient) -> None:
    with aioresponses() as m:
        m.get(TIMELINE, status=503, body="upstream unavailable")
        with pytest.raises(XApiError) as exc_info:
            await client.get_user_posts(
                "44196397", since_id=None, max_results=5, exclude=[], tweet_fields=[], expansions=[], user_fields=[]
            )
    assert exc_info.value.status == 503
    with aioresponses() as m:
        m.get(TIMELINE, status=200, body="not json")
        with pytest.raises(XApiError, match="not JSON"):
            await client.get_user_posts(
                "44196397", since_id=None, max_results=5, exclude=[], tweet_fields=[], expansions=[], user_fields=[]
            )


def test_client_requires_token() -> None:
    with pytest.raises(ValueError):
        XClient(SimpleNamespace(), "", XApiSettings())  # type: ignore[arg-type]
