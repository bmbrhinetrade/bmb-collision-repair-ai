"""Timeline polling: the fallback when the plan has no filtered stream, and gap recovery.

``TimelinePoller.run`` polls ``GET /2/users/:id/tweets`` for every verified account in
round-robin order, as fast as the app rate limit allows (``max_requests_per_15min *
utilization``), never faster than ``min_interval_seconds`` per account. ``since_id`` comes
from the store, or for an account we have never seen post, from the snowflake id of the
moment the poller was created, so the first request returns only new posts and we never
pay for a historical baseline.

``TimelinePoller.recover`` is a single catch-up pass used after a filtered-stream outage.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Callable
from datetime import datetime
from typing import Any

import aiohttp

from ..config import PollSettings, XApiSettings
from ..models import Account, IngestItem, IngestStatus, PostSource, Watchlist
from ..store import Store
from ..timeutil import datetime_to_snowflake, now_utc
from .client import EP_USER_TWEETS, CreditsDepleted, NotAuthorized, RateLimited, XApiError, XClient
from .stream import Backoff

log = logging.getLogger(__name__)

WINDOW_SECONDS = 900.0  # X rate-limit windows are 15 minutes
#: X ingest + scoring + push overhead on top of the polling delay (seconds).
POLL_OVERHEAD_SECONDS = 2.0
#: Consecutive failed requests before the poller reports itself down.
FAILURES_BEFORE_DOWN = 3
_MAX_ERROR_TEXT = 300


# --------------------------------------------------------------------------- pure helpers


def compute_poll_interval(n_accounts: int, settings: PollSettings) -> float:
    """Seconds between two polls of the same account (one full round-robin cycle).

    ``max(min_interval, n * 900 / (max_requests_per_15min * utilization))``
    """
    budget = settings.max_requests_per_15min * settings.utilization
    if budget <= 0:
        raise ValueError("poll.max_requests_per_15min * poll.utilization must be > 0")
    n = max(0, int(n_accounts))
    return max(float(settings.min_interval_seconds), n * WINDOW_SECONDS / budget)


def estimate_poll_latency(interval: float) -> tuple[float, float]:
    """(typical, worst) seconds from post to phone when polling every ``interval`` seconds.

    On average a post waits half a cycle for its account's next poll, at worst a full cycle,
    plus ~2 s for X, scoring and the push.
    """
    interval = max(0.0, float(interval))
    return (interval / 2.0 + POLL_OVERHEAD_SECONDS, interval + POLL_OVERHEAD_SECONDS)


def timeline_to_payloads(resp: dict, tag: str) -> list[dict]:
    """Split a timeline response (newest first) into stream-shaped payloads, oldest first."""
    if not isinstance(resp, dict):
        return []
    data = resp.get("data")
    if not isinstance(data, list):
        return []
    includes = resp.get("includes")
    if not isinstance(includes, dict):
        includes = {}
    posts = [p for p in data if isinstance(p, dict) and str(p.get("id", "")).isdigit()]
    if len(posts) != len(data):
        log.debug("timeline: dropped %d malformed post objects", len(data) - len(posts))
    posts.sort(key=lambda p: int(p["id"]))
    return [{"data": p, "includes": includes, "matching_rules": [{"id": "poll", "tag": tag}]} for p in posts]


def _describe_exc(exc: BaseException) -> str:
    text = str(exc).strip()
    desc = f"{type(exc).__name__}: {text}" if text else type(exc).__name__
    return " ".join(desc.split())[:_MAX_ERROR_TEXT]


class TokenBucket:
    """Request pacing. With capacity 1 this spaces requests evenly at ``rate`` per second.

    :meth:`try_acquire` is non-blocking: it takes a token and returns 0, or returns the
    number of seconds to wait. :meth:`pause_for` blocks all tokens (rate-limit resets,
    credits depleted, error backoff).
    """

    def __init__(self, rate: float, capacity: float = 1.0, clock: Callable[[], float] = time.monotonic) -> None:
        if rate <= 0:
            raise ValueError("rate must be > 0")
        self.rate = float(rate)
        self.capacity = max(1.0, float(capacity))
        self._clock = clock
        self._tokens = self.capacity
        self._updated = clock()
        self._paused_until = 0.0

    def try_acquire(self) -> float:
        now = self._clock()
        if now < self._paused_until:
            return self._paused_until - now
        if now > self._updated:
            self._tokens = min(self.capacity, self._tokens + (now - self._updated) * self.rate)
            self._updated = now
        if self._tokens >= 1.0:
            self._tokens -= 1.0
            return 0.0
        return (1.0 - self._tokens) / self.rate

    def pause_for(self, seconds: float) -> bool:
        """Hand out no tokens for ``seconds``. Returns True if this extended the pause."""
        until = self._clock() + max(0.0, seconds)
        if until > self._paused_until:
            self._paused_until = until
            return True
        return False

    @property
    def paused_seconds(self) -> float:
        return max(0.0, self._paused_until - self._clock())


async def _sleep_or_stop(stop_task: asyncio.Task[Any], seconds: float) -> bool:
    """Sleep up to ``seconds``; True if the stop task finished (stop requested)."""
    if stop_task.done():
        return True
    if seconds <= 0:
        return False
    done, _ = await asyncio.wait({stop_task}, timeout=seconds)
    return stop_task in done


# --------------------------------------------------------------------------- poller


class TimelinePoller:
    """Continuous timeline polling (``source="poll"``) and one-shot gap recovery."""

    def __init__(
        self,
        client: XClient,
        watchlist: Watchlist,
        settings: XApiSettings,
        queue: asyncio.Queue,
        status: IngestStatus,
        store: Store,
    ) -> None:
        self.client = client
        self.settings = settings
        self.queue = queue
        self.status = status
        self.store = store
        # Never poll an unverified handle: only accounts with a numeric user id.
        self.accounts: list[Account] = [a for a in watchlist.accounts if a.user_id]
        self.skipped_accounts = [a.handle for a in watchlist.accounts if not a.user_id]
        poll = settings.poll
        self.interval = compute_poll_interval(len(self.accounts), poll)
        self.latency = estimate_poll_latency(self.interval)
        self.started_at = now_utc()
        # since_id for accounts we have no stored id for: "anything posted after we started".
        self.baseline_since_id = str(datetime_to_snowflake(self.started_at))
        self.bucket = TokenBucket(rate=poll.max_requests_per_15min * poll.utilization / WINDOW_SECONDS)
        self.backoff = Backoff()
        self.requests = 0
        self.items_enqueued = 0
        self._failures = 0
        self._first_failure_at: datetime | None = None

    # ------------------------------------------------------------------ continuous polling

    async def run(self, stop: asyncio.Event) -> None:
        st = self.status
        st.mode = "poll"
        st.state = "polling"
        st.poll_interval_seconds = self.interval
        st.estimated_latency_seconds = self.latency[0]
        if not self.accounts:
            st.state = "down"
            st.last_error = "no verified accounts to poll (run `verify --write`)"
            log.error("poll: no accounts with a user_id; nothing to poll (run `verify --write`)")
            await stop.wait()
            st.state = "stopped"
            return
        if self.skipped_accounts:
            log.warning("poll: skipping %d unverified accounts without user_id", len(self.skipped_accounts))
        log.info(
            "poll: %d accounts, each every %.2fs (est. latency typical %.1fs, worst %.1fs)",
            len(self.accounts),
            self.interval,
            *self.latency,
        )
        loop = asyncio.get_running_loop()
        concurrency = max(1, int(self.settings.poll.concurrency))
        n = len(self.accounts)
        in_flight: dict[str, asyncio.Task[None]] = {}
        next_due: dict[str, float] = {a.user_id: 0.0 for a in self.accounts if a.user_id}
        stop_task: asyncio.Task[Any] = asyncio.create_task(stop.wait(), name="ceoscan-poll-stop")
        idx = 0
        try:
            while not stop_task.done():
                if len(in_flight) >= min(concurrency, n):
                    await asyncio.wait({stop_task, *in_flight.values()}, return_when=asyncio.FIRST_COMPLETED)
                    continue
                account = self.accounts[idx % n]
                idx += 1
                uid = account.user_id
                assert uid is not None
                if uid in in_flight:  # never two requests for one account (same since_id)
                    continue
                wait = next_due[uid] - loop.time()
                if wait > 0 and await _sleep_or_stop(stop_task, wait):
                    break
                if not await self._acquire(stop_task):
                    break
                next_due[uid] = loop.time() + self.interval
                task = asyncio.create_task(self._poll_one(account), name=f"ceoscan-poll-{account.handle}")
                in_flight[uid] = task
                task.add_done_callback(lambda _t, uid=uid: in_flight.pop(uid, None))
        finally:
            stop_task.cancel()
            pending = list(in_flight.values())
            for task in pending:
                task.cancel()
            await asyncio.gather(stop_task, *pending, return_exceptions=True)
            st.state = "stopped"
            log.info("poll: stopped (%d requests, %d posts)", self.requests, self.items_enqueued)

    async def _acquire(self, stop_task: asyncio.Task[Any]) -> bool:
        """Wait for a pacing token. False if stop was requested."""
        while True:
            wait = self.bucket.try_acquire()
            if wait <= 0:
                return True
            if await _sleep_or_stop(stop_task, wait):
                return False

    async def _poll_one(self, account: Account) -> None:
        try:
            await self._fetch(account, source="poll", max_results=self.settings.poll.max_results)
        except asyncio.CancelledError:
            raise
        except CreditsDepleted as exc:
            self._on_credits_depleted(exc)
        except RateLimited as exc:
            self._on_rate_limited(exc)
        except XApiError as exc:
            self._on_failure(account, str(exc), "http", auth=isinstance(exc, NotAuthorized))
        except (aiohttp.ClientError, OSError, TimeoutError) as exc:
            self._on_failure(account, _describe_exc(exc), "network")
        except Exception as exc:  # a bug must not stop polling
            log.exception("poll: unexpected error for @%s", account.handle)
            self._on_failure(account, _describe_exc(exc), "http")
        else:
            self._on_success()

    # ------------------------------------------------------------------ gap recovery

    async def recover(self) -> int:
        """One catch-up pass over all verified accounts; returns the number of posts enqueued.

        Uses each account's stored since_id (the newest post we processed). Accounts with no
        stored id use the poller's start baseline, which costs nothing when they have not
        posted (empty responses are not billed) and catches a first post made during the gap.
        """
        if not self.accounts:
            return 0
        max_results = max(5, min(100, int(self.settings.recovery.max_results)))
        sem = asyncio.Semaphore(max(1, int(self.settings.poll.concurrency)))
        total = 0
        aborted: str | None = None

        async def one(account: Account) -> None:
            nonlocal total, aborted
            async with sem:
                if aborted:
                    return
                try:
                    total += await self._fetch(account, source="recovery", max_results=max_results)
                except (CreditsDepleted, RateLimited) as exc:
                    aborted = str(exc)
                except XApiError as exc:
                    log.warning("recovery: @%s failed: %s", account.handle, exc)
                except (aiohttp.ClientError, OSError, TimeoutError) as exc:
                    log.warning("recovery: @%s failed: %s", account.handle, _describe_exc(exc))
                except Exception:
                    log.exception("recovery: unexpected error for @%s", account.handle)

        log.info("recovery: checking %d accounts for posts missed during the gap", len(self.accounts))
        await asyncio.gather(*(one(a) for a in self.accounts))
        if aborted:
            log.warning("recovery: stopped early: %s", aborted)
        log.info("recovery: %d missed posts enqueued", total)
        return total

    # ------------------------------------------------------------------ one request

    def _exclude(self, account: Account) -> list[str]:
        exclude: list[str] = []
        if not account.include_retweets:
            exclude.append("retweets")
        if not account.include_replies:
            exclude.append("replies")
        return exclude

    async def _fetch(self, account: Account, *, source: PostSource, max_results: int) -> int:
        uid = account.user_id
        assert uid is not None
        since_id = self.store.get_since_id(uid) or self.baseline_since_id
        resp = await self.client.get_user_posts(
            uid,
            since_id=since_id,
            max_results=max_results,
            exclude=self._exclude(account),
            tweet_fields=list(self.settings.tweet_fields),
            expansions=list(self.settings.expansions),
            user_fields=list(self.settings.user_fields),
        )
        self.requests += 1
        received_at = now_utc()
        tag = f"{self.settings.rules.tag_prefix}:{source}:{account.handle}"
        payloads = timeline_to_payloads(resp, tag)
        for payload in payloads:
            await self.queue.put(IngestItem(payload=payload, received_at=received_at, source=source))
        self.items_enqueued += len(payloads)
        meta = resp.get("meta") if isinstance(resp, dict) else None
        meta = meta if isinstance(meta, dict) else {}
        newest = meta.get("newest_id") or (payloads[-1]["data"]["id"] if payloads else None)
        if newest and str(newest).isdigit():
            self.store.set_since_id(uid, str(newest))
        if meta.get("next_token"):
            log.warning(
                "%s: @%s posted more than %d times since the last check; older posts were not fetched",
                source,
                account.handle,
                max_results,
            )
        if payloads:
            log.info("%s: @%s %d new post(s)", source, account.handle, len(payloads))
        self._honor_rate_limit()
        return len(payloads)

    def _honor_rate_limit(self) -> None:
        """Pause until the window resets when X says we are about to run out."""
        limits = getattr(self.client, "last_rate_limits", None) or {}
        rl = limits.get(EP_USER_TWEETS)
        if rl is None or rl.remaining is None or rl.reset_at is None:
            return
        reserve = max(1, int(self.settings.poll.concurrency))
        if rl.remaining > reserve:
            return
        wait = (rl.reset_at - now_utc()).total_seconds() + 1.0
        if wait > 0 and self.bucket.pause_for(wait):
            log.warning("poll: only %d requests left in this window; pausing %.0fs until reset", rl.remaining, wait)

    # ------------------------------------------------------------------ status

    def _on_success(self) -> None:
        st = self.status
        st.last_heartbeat_at = now_utc()
        if self._failures:
            log.info("poll: requests succeeding again after %d failure(s)", self._failures)
        self._failures = 0
        self._first_failure_at = None
        self.backoff.reset()
        st.down_since = None
        st.state = "polling"

    def _record_failure(self, msg: str) -> None:
        now = now_utc()
        self._failures += 1
        if self._first_failure_at is None:
            self._first_failure_at = now
        self.status.last_error = msg[:_MAX_ERROR_TEXT]
        if self._failures >= FAILURES_BEFORE_DOWN and self.status.down_since is None:
            self.status.down_since = self._first_failure_at

    def _on_failure(self, account: Account, msg: str, kind: str, auth: bool = False) -> None:
        self._record_failure(msg)
        if auth:
            log.error("poll: X rejected the credentials for @%s (%s); check X_BEARER_TOKEN", account.handle, msg)
        else:
            log.warning("poll: @%s failed: %s", account.handle, msg)
        if self._failures >= FAILURES_BEFORE_DOWN:
            if self.status.state != "credits_depleted":
                self.status.state = "down"
            delay = self.backoff.next_delay("network" if kind == "network" else "http")
            if delay > 0 and self.bucket.pause_for(delay):
                log.warning("poll: %d consecutive failures; pausing %.1fs", self._failures, delay)

    def _on_credits_depleted(self, exc: CreditsDepleted) -> None:
        self._record_failure(str(exc))
        st = self.status
        st.state = "credits_depleted"
        if st.down_since is None:
            st.down_since = self._first_failure_at or now_utc()
        retry = self.settings.stream.credits_depleted_retry_seconds
        if self.bucket.pause_for(retry):
            log.error("poll: X API credits depleted - add credits at console.x.com; retrying in %.0fs", retry)

    def _on_rate_limited(self, exc: RateLimited) -> None:
        self._record_failure(str(exc))
        reset_at = exc.rate_limit.reset_at if exc.rate_limit else None
        if reset_at is not None:
            delay = max(1.0, (reset_at - now_utc()).total_seconds() + 1.0)
        else:
            delay = self.backoff.next_delay("rate_limit")
        if self.bucket.pause_for(delay):
            log.warning("poll: rate limited (%s); pausing %.0fs", exc, delay)
