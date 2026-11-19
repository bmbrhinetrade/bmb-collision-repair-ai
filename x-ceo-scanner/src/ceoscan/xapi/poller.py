"""Timeline polling: the fallback when the plan has no filtered stream, and gap recovery.

``TimelinePoller.run`` polls ``GET /2/users/:id/tweets`` for every verified account in
round-robin order, as fast as the app rate limit allows (``max_requests_per_15min *
utilization``), never faster than ``min_interval_seconds`` per account. When X's
``x-rate-limit-limit`` header reports a different limit than the settings, the pacing (and the
latency reported in the status) adapts to the real limit.

Cursors (``since_id``) per account, in order of preference:

1. a *pending* cursor: snapshotted when a stream gap started (:meth:`snapshot_cursors`), or kept
   after a catch-up that did not finish, so live posts cannot move the cursor past missed ones
   (persisted in kv ``recovery_pending`` across restarts);
2. the store's since_id (the newest post we processed);
3. the baseline: the snowflake id of a "last alive" time (catch-up after a restart) or of the
   moment the poller was created, so the first request returns only new posts and we never pay
   for a historical baseline.

Every cursor is floored at ``now - recovery.max_age_seconds``: catch-up never fetches posts
older than that (cost and stale alerts after long outages). A request follows ``meta.next_token``
up to ``recovery.max_pages`` pages; the store's since_id only advances once every page was
fetched, and posts are enqueued oldest first.

``TimelinePoller.recover`` is the catch-up after a filtered-stream outage: one pass over every
account, then retries (with backoff) for the accounts that failed, until they succeed or the
retry window ends; whatever is still pending is retried by the next recovery.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import time
from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from typing import Any

import aiohttp

from ..config import PollSettings, XApiSettings
from ..models import Account, IngestItem, IngestStatus, PostSource, Watchlist
from ..store import Store
from ..timeutil import datetime_to_snowflake, now_utc
from .client import EP_USER_TWEETS, CreditsDepleted, NotAuthorized, RateLimited, XApiError, XClient
from .stream import Backoff, IngestEventLog

log = logging.getLogger(__name__)

WINDOW_SECONDS = 900.0  # X rate-limit windows are 15 minutes
#: X ingest + scoring + push overhead on top of the polling delay (seconds).
POLL_OVERHEAD_SECONDS = 2.0
#: Consecutive failed requests before the poller reports itself down.
FAILURES_BEFORE_DOWN = 3
#: kv key holding the pending catch-up cursors ({user_id: since_id}).
KV_RECOVERY_PENDING = "recovery_pending"
#: A rate-limit pause longer than this many poll intervals is called out in the log.
LONG_PAUSE_INTERVALS = 3.0
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


def age_floor_since_id(max_age_seconds: float, now: datetime | None = None) -> int | None:
    """Smallest since_id catch-up may use: the snowflake of ``now - max_age_seconds`` (None: no floor)."""
    if max_age_seconds is None or max_age_seconds <= 0:
        return None
    return datetime_to_snowflake((now or now_utc()) - timedelta(seconds=float(max_age_seconds)))


@dataclass
class _Pending:
    """A catch-up cursor that must be used instead of the store's since_id until it succeeds."""

    since_id: str
    epoch: int  # snapshot generation; a newer snapshot during a fetch keeps the entry pending


@dataclass
class _FetchResult:
    posts: int
    complete: bool  # every page was fetched (or the page cap was hit deliberately)


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
        *,
        catch_up_since: datetime | None = None,
        events: IngestEventLog | None = None,
        on_up: Callable[[], None] | None = None,
        on_pacing_change: Callable[[float, float], None] | None = None,
    ) -> None:
        """``catch_up_since``: when ingest last had coverage before this process started (the
        persisted last-alive time). Accounts with no stored since_id then catch up from there
        (bounded by ``recovery.max_age_seconds``) instead of from the poller's creation.
        ``on_up``: called after every successful poll request (continuous polling only).
        ``on_pacing_change(old_interval, new_interval)``: called when X's real rate limit changed
        the polling interval while polling.
        """
        self.client = client
        self.settings = settings
        self.queue = queue
        self.status = status
        self.store = store
        self.events = events if events is not None else IngestEventLog(store)
        self.on_up = on_up
        self.on_pacing_change = on_pacing_change
        # Never poll an unverified handle: only accounts with a numeric user id.
        self.accounts: list[Account] = [a for a in watchlist.accounts if a.user_id]
        self._by_uid: dict[str, Account] = {a.user_id: a for a in self.accounts if a.user_id}
        self.skipped_accounts = [a.handle for a in watchlist.accounts if not a.user_id]
        poll = settings.poll
        #: requests per 15 min for GET /2/users/:id/tweets: from settings until X tells us otherwise
        self.rate_limit = int(poll.max_requests_per_15min)
        self.interval = compute_poll_interval(len(self.accounts), poll)
        self.latency = estimate_poll_latency(self.interval)
        self.started_at = now_utc()
        # since_id for accounts we have no stored id for: "anything posted after we started", or
        # after the last moment we had coverage (restart catch-up), never older than the age floor.
        baseline_at = self.started_at
        if catch_up_since is not None and catch_up_since < baseline_at:
            baseline_at = catch_up_since
            max_age = float(settings.recovery.max_age_seconds)
            if max_age > 0:
                baseline_at = max(baseline_at, self.started_at - timedelta(seconds=max_age))
        self.baseline_at = baseline_at
        self.baseline_since_id = str(datetime_to_snowflake(baseline_at))
        self.bucket = TokenBucket(rate=poll.max_requests_per_15min * poll.utilization / WINDOW_SECONDS)
        self.backoff = Backoff()
        self.requests = 0
        self.items_enqueued = 0
        self._failures = 0
        self._first_failure_at: datetime | None = None
        self._polling = False  # True while run() is active (status / pacing announcements)
        # catch-up state
        self._pending: dict[str, _Pending] = {}
        self._epoch = 0
        self._recovering = False
        self._recovery_wake = asyncio.Event()
        #: retry schedule for accounts whose catch-up failed (seconds); instance attributes so
        #: tests can shrink them. The window is bounded by the age floor: older posts are skipped anyway.
        self.recovery_retry_base = 5.0
        self.recovery_retry_cap = 60.0
        max_age = float(settings.recovery.max_age_seconds)
        self.recovery_retry_window = min(max_age, 900.0) if max_age > 0 else 900.0
        self._load_pending()

    # ------------------------------------------------------------------ continuous polling

    async def run(self, stop: asyncio.Event) -> None:
        st = self.status
        st.mode = "poll"
        st.state = "polling"
        st.poll_interval_seconds = self.interval
        st.estimated_latency_seconds = self.latency[0]
        self._polling = True
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
            self._polling = False
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

    # ------------------------------------------------------------------ catch-up cursors

    def _stored_cursor(self, uid: str) -> str:
        return self.store.get_since_id(uid) or self.baseline_since_id

    def snapshot_cursors(self) -> None:
        """Pin every account's current cursor as *pending* (call when a stream gap starts).

        Synchronous on purpose: called before any await, live stream posts processed later can
        no longer move the cursor past posts missed during the gap. An account that is already
        pending keeps the older cursor (refetching overlap is deduped; skipping is not).
        """
        if not self.accounts:
            return
        self._epoch += 1
        for uid in self._by_uid:
            current = self._stored_cursor(uid)
            prev = self._pending.get(uid)
            if prev is not None and int(prev.since_id) <= int(current):
                current = prev.since_id
            self._pending[uid] = _Pending(current, self._epoch)
        self._save_pending()

    @property
    def pending_accounts(self) -> list[str]:
        """Handles whose catch-up has not completed yet."""
        return [self._by_uid[uid].handle for uid in self._pending if uid in self._by_uid]

    def _clear_pending(self, uid: str, epoch: int | None) -> None:
        entry = self._pending.get(uid)
        if entry is not None and entry.epoch == epoch:
            del self._pending[uid]
            self._save_pending()

    def _keep_pending(self, uid: str, since_id: str) -> None:
        """Remember ``since_id`` for ``uid`` until a fetch from it completes (never moves forward)."""
        entry = self._pending.get(uid)
        if entry is not None and int(entry.since_id) <= int(since_id):
            return
        self._pending[uid] = _Pending(since_id, entry.epoch if entry is not None else self._epoch)
        self._save_pending()

    def _load_pending(self) -> None:
        try:
            raw = self.store.kv_get(KV_RECOVERY_PENDING)
            data = json.loads(raw) if raw else {}
        except Exception:  # a corrupt value only costs the restart catch-up of a failed recovery
            log.warning("recovery: ignoring unreadable %s state", KV_RECOVERY_PENDING)
            return
        if not isinstance(data, dict):
            return
        for uid, since_id in data.items():
            if uid in self._by_uid and str(since_id).isdigit():
                self._pending[str(uid)] = _Pending(str(since_id), self._epoch)
        if self._pending:
            log.info("recovery: %d account(s) still have an unfinished catch-up from the last run", len(self._pending))

    def _save_pending(self) -> None:
        try:
            self.store.kv_set(KV_RECOVERY_PENDING, json.dumps({u: e.since_id for u, e in self._pending.items()}))
        except Exception:  # bookkeeping must never break ingest
            log.debug("recovery: could not persist the pending cursors", exc_info=True)

    def _floored(self, since_id: str) -> str:
        """``since_id`` raised to the age floor (never fetch posts older than recovery.max_age_seconds)."""
        floor = age_floor_since_id(self.settings.recovery.max_age_seconds)
        if floor is not None and int(since_id) < floor:
            return str(floor)
        return since_id

    # ------------------------------------------------------------------ gap recovery

    async def recover(self) -> int | None:
        """Catch up on posts missed during a stream outage; returns the number of posts enqueued.

        Uses each account's pending cursor (snapshotted when the gap started) or else its stored
        since_id; accounts with neither use the baseline, which costs nothing when they have not
        posted (empty responses are not billed) and catches a first post made during the gap.
        Accounts that fail are retried with backoff within ``recovery_retry_window``; whatever is
        still pending after that is retried by the next recovery (next reconnect or restart).

        If a recovery is already running, the new gap is merged into it and None is returned.
        """
        if not self.accounts:
            return 0
        self.snapshot_cursors()  # before any await: live posts must not move these cursors
        if self._recovering:
            self._recovery_wake.set()
            log.info("recovery: already running; the new gap is included in it")
            return None
        self._recovering = True
        try:
            return await self._recover_until_done()
        finally:
            self._recovering = False

    async def _recover_until_done(self) -> int:
        loop = asyncio.get_running_loop()
        deadline = loop.time() + max(0.0, float(self.recovery_retry_window))
        floor = age_floor_since_id(self.settings.recovery.max_age_seconds)
        log.info(
            "recovery: checking %d accounts for posts missed during the gap%s",
            len(self._pending),
            f" (not older than {self.settings.recovery.max_age_seconds:.0f}s)" if floor is not None else "",
        )
        total = 0
        attempt = 0
        while True:
            self._recovery_wake.clear()
            n, wait_hint = await self._recover_pass()
            total += n
            remaining = self.pending_accounts
            if not remaining:
                break
            attempt += 1
            delay = wait_hint if wait_hint is not None else self.recovery_retry_base * 2 ** (attempt - 1)
            delay = max(0.0, min(delay, max(self.recovery_retry_cap, wait_hint or 0.0)))
            if loop.time() + delay > deadline:
                msg = (
                    f"{len(remaining)} account(s) not caught up ({', '.join('@' + h for h in remaining[:5])}"
                    f"{', ...' if len(remaining) > 5 else ''}); retried at the next reconnect or restart"
                )
                log.warning("recovery: %s", msg)
                self.events.error("poll_error", f"recovery: {msg}")
                break
            log.info("recovery: %d account(s) not caught up yet; retrying in %.0fs", len(remaining), delay)
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self._recovery_wake.wait(), timeout=delay)
        log.info("recovery: %d missed posts enqueued", total)
        return total

    async def _recover_pass(self) -> tuple[int, float | None]:
        """One pass over the pending accounts. Returns (posts enqueued, wait hint for a retry)."""
        max_results = max(5, min(100, int(self.settings.recovery.max_results)))
        sem = asyncio.Semaphore(max(1, int(self.settings.poll.concurrency)))
        total = 0
        aborted: XApiError | None = None
        work = [self._by_uid[uid] for uid in list(self._pending) if uid in self._by_uid]

        async def one(account: Account) -> None:
            nonlocal total, aborted
            async with sem:
                if aborted is not None:
                    return
                try:
                    # Await first: `total += await ...` would read `total` before the await and
                    # lose counts from concurrent passes.
                    n = await self._fetch(account, source="recovery", max_results=max_results)
                    total += n
                except (CreditsDepleted, RateLimited) as exc:
                    aborted = exc
                except XApiError as exc:
                    log.warning("recovery: @%s failed: %s", account.handle, exc)
                    self.events.error("poll_error", f"recovery @{account.handle}: {exc}")
                except (aiohttp.ClientError, OSError, TimeoutError) as exc:
                    log.warning("recovery: @%s failed: %s", account.handle, _describe_exc(exc))
                    self.events.error("poll_error", f"recovery @{account.handle}: {_describe_exc(exc)}")
                except Exception as exc:
                    log.exception("recovery: unexpected error for @%s", account.handle)
                    self.events.error("poll_error", f"recovery @{account.handle}: {_describe_exc(exc)}")

        await asyncio.gather(*(one(a) for a in work))
        wait_hint: float | None = None
        if aborted is not None:
            log.warning("recovery: stopped early: %s", aborted)
            self.events.error("poll_error", f"recovery stopped early: {aborted}")
            if isinstance(aborted, CreditsDepleted):
                wait_hint = float(self.settings.stream.credits_depleted_retry_seconds)
            elif aborted.rate_limit is not None and aborted.rate_limit.reset_at is not None:
                wait_hint = max(1.0, (aborted.rate_limit.reset_at - now_utc()).total_seconds() + 1.0)
        return total, wait_hint

    # ------------------------------------------------------------------ one account

    def _exclude(self, account: Account) -> list[str]:
        exclude: list[str] = []
        if not account.include_retweets:
            exclude.append("retweets")
        if not account.include_replies:
            exclude.append("replies")
        return exclude

    async def _fetch(self, account: Account, *, source: PostSource, max_results: int) -> int:
        """Fetch everything newer than the account's cursor (all pages), enqueue it oldest first.

        Raises when the first page fails. When a later page fails, what was fetched is enqueued
        (newest posts matter most) and the cursor stays pending, so the next fetch starts from
        the same place and the older posts are not skipped.
        """
        uid = account.user_id
        assert uid is not None
        entry = self._pending.get(uid)
        cursor = entry.since_id if entry is not None else self._stored_cursor(uid)
        epoch = entry.epoch if entry is not None else None
        since_id = self._floored(cursor)
        result = await self._fetch_pages(account, since_id, source=source, max_results=max_results)
        if result.complete:
            self._clear_pending(uid, epoch)
        else:
            self._keep_pending(uid, cursor)
        return result.posts

    async def _fetch_pages(self, account: Account, since_id: str, *, source: PostSource, max_results: int) -> _FetchResult:
        uid = account.user_id
        assert uid is not None
        max_pages = max(1, int(self.settings.recovery.max_pages))
        tag = f"{self.settings.rules.tag_prefix}:{source}:{account.handle}"
        payloads: dict[str, dict] = {}
        newest: str | None = None
        token: str | None = None
        complete = True
        capped = False
        for page in range(max_pages):
            try:
                resp = await self.client.get_user_posts(
                    uid,
                    since_id=since_id,
                    max_results=max_results,
                    exclude=self._exclude(account),
                    tweet_fields=list(self.settings.tweet_fields),
                    expansions=list(self.settings.expansions),
                    user_fields=list(self.settings.user_fields),
                    **({"pagination_token": token} if token else {}),
                )
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                if page == 0:
                    raise
                complete = False
                log.warning(
                    "%s: @%s: page %d failed (%s); %d post(s) fetched so far are processed, the rest is retried",
                    source, account.handle, page + 1, _describe_exc(exc), len(payloads),
                )
                self.events.error("poll_error", f"{source} @{account.handle} page {page + 1}: {_describe_exc(exc)}")
                break
            self.requests += 1
            self._learn_rate_limit()
            meta = resp.get("meta") if isinstance(resp, dict) else None
            meta = meta if isinstance(meta, dict) else {}
            page_payloads = timeline_to_payloads(resp, tag)
            if page == 0:
                newest = meta.get("newest_id") or (page_payloads[-1]["data"]["id"] if page_payloads else None)
            for payload in page_payloads:
                payloads.setdefault(payload["data"]["id"], payload)
            token = meta.get("next_token") or None
            if not token:
                break
        else:
            capped = token is not None
        received_at = now_utc()
        ordered = sorted(payloads.values(), key=lambda p: int(p["data"]["id"]))
        for payload in ordered:
            await self.queue.put(IngestItem(payload=payload, received_at=received_at, source=source))
        self.items_enqueued += len(ordered)
        if complete and newest and str(newest).isdigit():
            self.store.set_since_id(uid, str(newest))
        if capped:
            msg = (
                f"@{account.handle} posted more than {max_pages * max_results} times since the last check; "
                f"the oldest were not fetched (raise x_api.recovery.max_pages to fetch more)"
            )
            log.warning("%s: %s", source, msg)
            self.events.error("catchup_truncated", f"{source}: {msg}", level="warning")
        if ordered:
            log.info("%s: @%s %d new post(s)", source, account.handle, len(ordered))
        self._honor_rate_limit()
        return _FetchResult(len(ordered), complete)

    # ------------------------------------------------------------------ rate limits

    def _learn_rate_limit(self) -> None:
        """Adapt the pacing when X reports a different timeline limit than the settings assume."""
        limits = getattr(self.client, "last_rate_limits", None) or {}
        rl = limits.get(EP_USER_TWEETS)
        limit = rl.limit if rl is not None else None
        if limit is None or limit <= 0 or limit == self.rate_limit:
            return
        poll = replace(self.settings.poll, max_requests_per_15min=int(limit))
        old_interval, old_limit = self.interval, self.rate_limit
        self.rate_limit = int(limit)
        self.interval = compute_poll_interval(len(self.accounts), poll)
        self.latency = estimate_poll_latency(self.interval)
        self.bucket.rate = poll.max_requests_per_15min * poll.utilization / WINDOW_SECONDS
        log.warning(
            "poll: X reports a limit of %d timeline requests / 15 min (settings: %d); each account is now "
            "checked every %.1fs (est. latency typical %.1fs, worst %.1fs)",
            limit,
            old_limit,
            self.interval,
            *self.latency,
        )
        if self._polling:
            self.status.poll_interval_seconds = self.interval
            self.status.estimated_latency_seconds = self.latency[0]
            if self.on_pacing_change is not None and abs(self.interval - old_interval) > 1e-9:
                try:
                    self.on_pacing_change(old_interval, self.interval)
                except Exception:
                    log.exception("poll: on_pacing_change callback failed")

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
            if self._polling and wait > LONG_PAUSE_INTERVALS * max(self.interval, 1.0):
                log.warning(
                    "poll: alerts may be up to %.0fs late until the rate-limit window resets "
                    "(x_api.poll.max_requests_per_15min=%d may be higher than this plan allows)",
                    wait + self.interval,
                    self.settings.poll.max_requests_per_15min,
                )

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
        if self.on_up is not None:
            try:
                self.on_up()
            except Exception:
                log.exception("poll: on_up callback failed")

    def _record_failure(self, msg: str) -> None:
        now = now_utc()
        self._failures += 1
        if self._first_failure_at is None:
            self._first_failure_at = now
        self.status.last_error = msg[:_MAX_ERROR_TEXT]
        if self._failures >= FAILURES_BEFORE_DOWN and self.status.down_since is None:
            self.status.down_since = self._first_failure_at
        self.events.error("poll_error", msg)

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
