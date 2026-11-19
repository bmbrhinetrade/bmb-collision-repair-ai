"""Tests for ceoscan.xapi.poller (timeline polling fallback + gap recovery)."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from datetime import datetime, timedelta
from typing import Any

import aiohttp
import pytest

from ceoscan.config import PollSettings, RecoverySettings, StreamSettings, XApiSettings
from ceoscan.models import Account, IngestStatus, Watchlist
from ceoscan.store import Store
from ceoscan.timeutil import datetime_to_snowflake, now_utc
from ceoscan.xapi.client import EP_USER_TWEETS, CreditsDepleted, RateLimited, RateLimitInfo
from ceoscan.xapi.poller import (
    TimelinePoller,
    TokenBucket,
    compute_poll_interval,
    estimate_poll_latency,
    timeline_to_payloads,
)

# --------------------------------------------------------------------------- pure functions


def test_compute_poll_interval_math():
    p = PollSettings()  # 10,000 / 15 min at 80% -> 8,000 requests per 900 s
    assert compute_poll_interval(19, p) == pytest.approx(19 * 900 / 8000)  # 2.1375 s
    assert compute_poll_interval(100, p) == pytest.approx(11.25)
    assert compute_poll_interval(1, p) == 2.0  # floored at min_interval_seconds
    assert compute_poll_interval(0, p) == 2.0
    p2 = PollSettings(max_requests_per_15min=1500, utilization=1.0, min_interval_seconds=0.5)
    assert compute_poll_interval(10, p2) == pytest.approx(6.0)
    with pytest.raises(ValueError):
        compute_poll_interval(3, PollSettings(utilization=0.0))


def test_estimate_poll_latency():
    assert estimate_poll_latency(2.0) == (3.0, 4.0)
    typical, worst = estimate_poll_latency(11.25)
    assert typical == pytest.approx(7.625) and worst == pytest.approx(13.25)
    assert typical < worst


def _timeline(user_id: str, ids: list[int], username: str = "someone", next_token: bool = False) -> dict:
    data = [
        {"id": str(i), "text": f"post {i}", "author_id": user_id, "edit_history_tweet_ids": [str(i)]}
        for i in sorted(ids, reverse=True)  # X returns newest first
    ]
    meta: dict[str, Any] = {"result_count": len(data)}
    if data:
        meta.update(newest_id=data[0]["id"], oldest_id=data[-1]["id"])
    if next_token:
        meta["next_token"] = "abc"
    return {"data": data, "includes": {"users": [{"id": user_id, "username": username}]}, "meta": meta}


def test_timeline_to_payloads_oldest_first_stream_shaped():
    resp = _timeline("42", [100, 99, 1000, 101], "LisaSu")
    payloads = timeline_to_payloads(resp, "ceoscan:poll:LisaSu")
    assert [p["data"]["id"] for p in payloads] == ["99", "100", "101", "1000"]  # numeric, not lexical
    for p in payloads:
        assert set(p) == {"data", "includes", "matching_rules"}
        assert p["includes"] == resp["includes"]
        assert p["matching_rules"] == [{"id": "poll", "tag": "ceoscan:poll:LisaSu"}]


def test_timeline_to_payloads_edge_cases():
    assert timeline_to_payloads({"meta": {"result_count": 0}}, "t") == []
    assert timeline_to_payloads({}, "t") == []
    assert timeline_to_payloads({"data": "nope"}, "t") == []
    assert timeline_to_payloads(None, "t") == []  # type: ignore[arg-type]
    out = timeline_to_payloads({"data": [{"id": "5"}, {"text": "no id"}, "junk", {"id": "x1"}]}, "t")
    assert [p["data"]["id"] for p in out] == ["5"]
    assert out[0]["includes"] == {}


def test_token_bucket_pacing_and_pause():
    t = [0.0]
    b = TokenBucket(rate=2.0, clock=lambda: t[0])
    assert b.try_acquire() == 0.0
    assert b.try_acquire() == pytest.approx(0.5)
    t[0] = 0.5
    assert b.try_acquire() == 0.0
    t[0] = 100.0  # long idle: capacity 1 means no burst afterwards
    assert b.try_acquire() == 0.0
    assert b.try_acquire() == pytest.approx(0.5)
    assert b.pause_for(30.0) is True
    assert b.try_acquire() == pytest.approx(30.0)
    assert b.pause_for(5.0) is False  # never shortens an existing pause
    assert b.paused_seconds == pytest.approx(30.0)
    t[0] = 130.0
    assert b.try_acquire() == 0.0
    with pytest.raises(ValueError):
        TokenBucket(rate=0)


# --------------------------------------------------------------------------- fake client


class FakeTimelineClient:
    """Stands in for XClient.get_user_posts; per-user scripted responses or exceptions."""

    def __init__(self, responses: dict[str, list[Any]] | None = None, default: Any = None) -> None:
        self.responses = responses or {}
        self.default = default
        self.calls: list[dict[str, Any]] = []
        self.last_rate_limits: dict[str, RateLimitInfo] = {}
        self.on_call: Callable[[dict[str, Any]], None] | None = None

    async def get_user_posts(self, user_id, *, since_id, max_results, exclude, tweet_fields, expansions, user_fields):
        call = {"user_id": user_id, "since_id": since_id, "max_results": max_results, "exclude": list(exclude),
                "tweet_fields": list(tweet_fields), "expansions": list(expansions), "user_fields": list(user_fields)}
        self.calls.append(call)
        if self.on_call:
            self.on_call(call)
        await asyncio.sleep(0)
        queue = self.responses.get(user_id)
        result = queue.pop(0) if queue else self.default
        if isinstance(result, BaseException):
            raise result
        if callable(result):
            return result(call)
        return result if result is not None else {"meta": {"result_count": 0}}

    def calls_for(self, user_id: str) -> list[dict[str, Any]]:
        return [c for c in self.calls if c["user_id"] == user_id]


WATCHLIST = Watchlist(
    accounts=(
        Account(handle="LisaSu", tickers=("AMD",), user_id="111", include_replies=True),
        Account(handle="AMD", tickers=("AMD",), kind="company", user_id="222", include_replies=False),
        Account(handle="unverified", tickers=("AMD",)),  # no user_id -> never polled
        Account(handle="elonmusk", tickers=("TSLA",), user_id="333", include_retweets=True),
    ),
    keyword_rules=(),
    my_tickers=("AMD", "TSLA"),
    company_names={},
)


def fast_settings(**poll_kw: Any) -> XApiSettings:
    poll = PollSettings(**{"max_requests_per_15min": 900_000, "utilization": 1.0, "min_interval_seconds": 0.05,
                           "concurrency": 2, "max_results": 10, **poll_kw})
    return XApiSettings(poll=poll, recovery=RecoverySettings(max_results=20),
                        stream=StreamSettings(credits_depleted_retry_seconds=60.0))


def drain(queue: asyncio.Queue) -> list:
    items = []
    while not queue.empty():
        items.append(queue.get_nowait())
    return items


async def wait_until(cond: Callable[[], bool], timeout: float = 5.0) -> None:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while not cond():
        if loop.time() > deadline:
            raise AssertionError("condition not met in time")
        await asyncio.sleep(0.005)


async def stop_and_join(stop: asyncio.Event, task: asyncio.Task) -> None:
    stop.set()
    await asyncio.wait_for(task, timeout=5.0)


@pytest.fixture
def store():
    s = Store(":memory:")
    yield s
    s.close()


# --------------------------------------------------------------------------- run()


async def test_poller_run_updates_since_id_and_enqueues(store):
    base = datetime_to_snowflake(now_utc())
    store.set_since_id("111", str(base - 500))  # LisaSu: seen before
    client = FakeTimelineClient(
        {
            "111": [_timeline("111", [base + 2, base + 1], "LisaSu")],
            "222": [_timeline("222", [base + 5], "AMD")],
        }
    )
    queue: asyncio.Queue = asyncio.Queue()
    status = IngestStatus()
    settings = fast_settings()
    poller = TimelinePoller(client, WATCHLIST, settings, queue, status, store)  # type: ignore[arg-type]
    assert [a.handle for a in poller.accounts] == ["LisaSu", "AMD", "elonmusk"]

    stop = asyncio.Event()
    task = asyncio.create_task(poller.run(stop))
    await wait_until(lambda: all(len(client.calls_for(u)) >= 2 for u in ("111", "222", "333")))
    await stop_and_join(stop, task)

    lisa, amd, elon = client.calls_for("111"), client.calls_for("222"), client.calls_for("333")
    # since_id: stored id first, then the newest id we got back
    assert lisa[0]["since_id"] == str(base - 500)
    assert lisa[1]["since_id"] == str(base + 2)
    # no stored id -> the poller's start snowflake (no paid baseline fetch)
    assert amd[0]["since_id"] == poller.baseline_since_id
    assert int(poller.baseline_since_id) == datetime_to_snowflake(poller.started_at)
    assert amd[1]["since_id"] == str(base + 5)
    assert elon[0]["since_id"] == poller.baseline_since_id
    assert store.get_since_id("111") == str(base + 2)
    assert store.get_since_id("222") == str(base + 5)
    assert store.get_since_id("333") is None  # nothing posted -> nothing stored

    # exclude retweets unless include_retweets, replies unless include_replies
    assert lisa[0]["exclude"] == ["retweets"]
    assert amd[0]["exclude"] == ["retweets", "replies"]
    assert elon[0]["exclude"] == []
    assert lisa[0]["max_results"] == 10
    assert lisa[0]["tweet_fields"] == settings.tweet_fields
    assert lisa[0]["expansions"] == settings.expansions

    items = drain(queue)
    assert [i.payload["data"]["id"] for i in items if i.payload["data"]["author_id"] == "111"] == [
        str(base + 1), str(base + 2)]  # oldest first
    assert len(items) == 3
    assert all(i.source == "poll" for i in items)
    assert items[0].payload["matching_rules"][0]["tag"].startswith("ceoscan:poll:")
    assert poller.items_enqueued == 3

    assert status.mode == "poll"
    assert status.poll_interval_seconds == pytest.approx(0.05)
    assert status.estimated_latency_seconds == pytest.approx(0.05 / 2 + 2)
    assert status.last_heartbeat_at is not None
    assert status.down_since is None
    assert status.state == "stopped"


async def test_poller_status_polling_while_running(store):
    client = FakeTimelineClient()
    status = IngestStatus()
    poller = TimelinePoller(client, WATCHLIST, fast_settings(), asyncio.Queue(), status, store)  # type: ignore[arg-type]
    stop = asyncio.Event()
    task = asyncio.create_task(poller.run(stop))
    await wait_until(lambda: len(client.calls) >= 3)
    assert status.state == "polling" and status.mode == "poll"
    await stop_and_join(stop, task)


async def test_poller_paces_requests_under_budget(store):
    # 18,000 / 15 min = 20 requests/s; 3 accounts -> each every 0.15 s
    settings = fast_settings(max_requests_per_15min=18_000, min_interval_seconds=0.01, concurrency=4)
    client = FakeTimelineClient()
    poller = TimelinePoller(client, WATCHLIST, settings, asyncio.Queue(), IngestStatus(), store)  # type: ignore[arg-type]
    assert poller.interval == pytest.approx(0.15)
    stop = asyncio.Event()
    task = asyncio.create_task(poller.run(stop))
    await asyncio.sleep(0.6)
    await stop_and_join(stop, task)
    n = len(client.calls)
    assert 6 <= n <= 14  # ~12 at 20/s; never a burst
    # round robin: every account polled, none twice in a row
    users = [c["user_id"] for c in client.calls]
    assert set(users) == {"111", "222", "333"}
    assert all(a != b for a, b in zip(users, users[1:]))


async def test_poller_min_interval_per_account(store):
    one = Watchlist(accounts=(WATCHLIST.accounts[0],), keyword_rules=(), my_tickers=("AMD",), company_names={})
    settings = fast_settings(min_interval_seconds=0.2, concurrency=4)
    client = FakeTimelineClient()
    poller = TimelinePoller(client, one, settings, asyncio.Queue(), IngestStatus(), store)  # type: ignore[arg-type]
    stop = asyncio.Event()
    task = asyncio.create_task(poller.run(stop))
    await asyncio.sleep(0.5)
    await stop_and_join(stop, task)
    assert 2 <= len(client.calls) <= 3  # t=0, 0.2, 0.4 - not 4 concurrent requests


async def test_poller_credits_depleted_waits(store):
    one = Watchlist(accounts=(WATCHLIST.accounts[0],), keyword_rules=(), my_tickers=("AMD",), company_names={})
    client = FakeTimelineClient(default=CreditsDepleted(status=402, title="CreditsDepleted", detail="no credits"))
    status = IngestStatus()
    poller = TimelinePoller(client, one, fast_settings(), asyncio.Queue(), status, store)  # type: ignore[arg-type]
    stop = asyncio.Event()
    task = asyncio.create_task(poller.run(stop))
    await wait_until(lambda: status.state == "credits_depleted")
    await asyncio.sleep(0.2)
    assert len(client.calls) == 1  # waiting credits_depleted_retry_seconds
    assert status.down_since is not None
    assert "402" in (status.last_error or "")
    assert poller.bucket.paused_seconds > 50
    await stop_and_join(stop, task)


async def test_poller_down_after_three_failures_then_recovers(store):
    one = Watchlist(accounts=(WATCHLIST.accounts[0],), keyword_rules=(), my_tickers=("AMD",), company_names={})
    err = aiohttp.ClientConnectionError("connection refused")
    base = datetime_to_snowflake(now_utc())
    client = FakeTimelineClient({"111": [err, err, err, _timeline("111", [base + 1])]})
    status = IngestStatus()
    seen: list[tuple[str, datetime | None]] = []
    client.on_call = lambda call: seen.append((status.state, status.down_since))
    queue: asyncio.Queue = asyncio.Queue()
    poller = TimelinePoller(client, one, fast_settings(min_interval_seconds=0.01), queue, status, store)  # type: ignore[arg-type]
    stop = asyncio.Event()
    task = asyncio.create_task(poller.run(stop))
    await wait_until(lambda: len(client.calls) >= 5)
    await stop_and_join(stop, task)
    # before the 3rd failure: not down yet; after it: down with down_since set
    assert seen[0] == ("polling", None)
    assert seen[2][1] is None
    assert seen[3][0] == "down" and seen[3][1] is not None
    # the 4th request succeeded -> healthy again
    assert seen[4] == ("polling", None)
    assert queue.qsize() == 1
    assert status.last_error and "ClientConnectionError" in status.last_error


async def test_poller_pauses_when_rate_limit_nearly_exhausted(store):
    client = FakeTimelineClient()
    reset_at = now_utc() + timedelta(seconds=120)

    def low_remaining(call: dict) -> None:
        client.last_rate_limits[EP_USER_TWEETS] = RateLimitInfo(limit=10_000, remaining=1, reset_at=reset_at)

    client.on_call = low_remaining
    poller = TimelinePoller(client, WATCHLIST, fast_settings(concurrency=1), asyncio.Queue(), IngestStatus(), store)  # type: ignore[arg-type]
    stop = asyncio.Event()
    task = asyncio.create_task(poller.run(stop))
    await wait_until(lambda: len(client.calls) >= 1)
    await asyncio.sleep(0.2)
    assert len(client.calls) == 1
    assert 110 < poller.bucket.paused_seconds <= 122
    await stop_and_join(stop, task)


async def test_poller_rate_limited_pauses_until_reset(store):
    reset_at = now_utc() + timedelta(seconds=300)
    exc = RateLimited(status=429, title="Too Many Requests",
                      rate_limit=RateLimitInfo(limit=10_000, remaining=0, reset_at=reset_at))
    client = FakeTimelineClient(default=exc)
    status = IngestStatus()
    poller = TimelinePoller(client, WATCHLIST, fast_settings(concurrency=1), asyncio.Queue(), status, store)  # type: ignore[arg-type]
    stop = asyncio.Event()
    task = asyncio.create_task(poller.run(stop))
    await wait_until(lambda: len(client.calls) >= 1)
    await asyncio.sleep(0.2)
    assert len(client.calls) == 1
    assert 290 < poller.bucket.paused_seconds <= 302
    assert "429" in (status.last_error or "")
    await stop_and_join(stop, task)


async def test_poller_without_verified_accounts_waits_for_stop(store):
    wl = Watchlist(accounts=(Account(handle="nobody", tickers=("AMD",)),), keyword_rules=(),
                   my_tickers=("AMD",), company_names={})
    client = FakeTimelineClient()
    status = IngestStatus()
    poller = TimelinePoller(client, wl, fast_settings(), asyncio.Queue(), status, store)  # type: ignore[arg-type]
    stop = asyncio.Event()
    task = asyncio.create_task(poller.run(stop))
    await asyncio.sleep(0.05)
    assert not task.done() and status.state == "down"
    await stop_and_join(stop, task)
    assert client.calls == []
    assert await poller.recover() == 0


async def test_poller_warns_when_more_posts_than_max_results(store, caplog):
    base = datetime_to_snowflake(now_utc())
    client = FakeTimelineClient({"111": [_timeline("111", list(range(base + 1, base + 11)), next_token=True)]})
    one = Watchlist(accounts=(WATCHLIST.accounts[0],), keyword_rules=(), my_tickers=("AMD",), company_names={})
    queue: asyncio.Queue = asyncio.Queue()
    poller = TimelinePoller(client, one, fast_settings(), queue, IngestStatus(), store)  # type: ignore[arg-type]
    stop = asyncio.Event()
    with caplog.at_level(logging.WARNING, logger="ceoscan.xapi.poller"):
        task = asyncio.create_task(poller.run(stop))
        await wait_until(lambda: queue.qsize() == 10)
        await stop_and_join(stop, task)
    assert "older posts were not fetched" in caplog.text


# --------------------------------------------------------------------------- recover()


async def test_recover_one_pass_with_since_ids(store):
    base = datetime_to_snowflake(now_utc())
    store.set_since_id("111", str(base - 10))
    store.set_since_id("333", str(base - 20))
    client = FakeTimelineClient(
        {
            "111": [_timeline("111", [base + 3, base + 2], "LisaSu")],
            "333": [_timeline("333", [base + 7], "elonmusk")],
        }
    )
    queue: asyncio.Queue = asyncio.Queue()
    status = IngestStatus(mode="stream", state="connected")
    poller = TimelinePoller(client, WATCHLIST, fast_settings(), queue, status, store)  # type: ignore[arg-type]
    n = await poller.recover()
    assert n == 3
    by_user = {c["user_id"]: c for c in client.calls}
    assert len(client.calls) == 3  # one pass, each verified account once
    assert by_user["111"]["since_id"] == str(base - 10)
    assert by_user["333"]["since_id"] == str(base - 20)
    assert by_user["222"]["since_id"] == poller.baseline_since_id
    assert all(c["max_results"] == 20 for c in client.calls)  # recovery.max_results
    items = drain(queue)
    assert len(items) == 3 and all(i.source == "recovery" for i in items)
    assert store.get_since_id("111") == str(base + 3)
    assert store.get_since_id("333") == str(base + 7)
    # recovery never touches the stream's status
    assert (status.mode, status.state) == ("stream", "connected")


async def test_recover_stops_early_on_credits_and_never_raises(store):
    client = FakeTimelineClient(default=CreditsDepleted(status=402, title="CreditsDepleted"))
    poller = TimelinePoller(client, WATCHLIST, fast_settings(concurrency=1), asyncio.Queue(), IngestStatus(), store)  # type: ignore[arg-type]
    assert await poller.recover() == 0
    assert len(client.calls) == 1  # aborted after the first 402


async def test_recover_skips_failing_accounts(store):
    base = datetime_to_snowflake(now_utc())
    client = FakeTimelineClient(
        {
            "111": [aiohttp.ClientConnectionError("boom")],
            "222": [_timeline("222", [base + 1])],
            "333": [asyncio.TimeoutError()],
        }
    )
    poller = TimelinePoller(client, WATCHLIST, fast_settings(), asyncio.Queue(), IngestStatus(), store)  # type: ignore[arg-type]
    assert await poller.recover() == 1
