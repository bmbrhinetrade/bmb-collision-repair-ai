"""Red-flag tracker: who is posting bad news about my tickers, and does the company deny it?

Every scored post that Claude (or the rules-only fallback) marks as a red flag about one of my tickers is
recorded per ticker in the store (table ``red_flag_events``: time, author, handle, post id, credibility level,
types, claim status, impact; never post text), so the picture survives restarts. From that:

* :meth:`RedFlagTracker.observe` returns a :class:`~ceoscan.models.RedFlagSignal` for the post: how many distinct
  authors posted bad news about the ticker within ``red_flags.spread_window_minutes`` (by X post time), which
  official / trusted accounts reported it, and ``spreading`` once ``red_flags.spread_min_authors`` is reached and
  no "spreading" alert went out for that ticker in the last ``red_flags.spread_cooldown_minutes``. Posts are scored
  in parallel and finish out of X-time order, so the count is the busiest window of that length that contains the
  post - events before AND after it - not only the posts that happen to be recorded already. The cooldown is
  claimed at once (kv ``red_flag_spread:<TICKER>``, so two posts processed in parallel cannot both alert) and
  handed back with :meth:`release_spread` if the alert could not be delivered (or was not sent).
* An author counts as "official" only for the watched account's OWN tickers, and as a confirmation (``confirmed_by``)
  only when its claim is CONFIRMED (official) or CREDIBLE: a CEO's allegation about another company, or an
  UNVERIFIED post by a large account, confirms nothing.
* :meth:`denial_tickers` / :meth:`find_denial`: a watched account's denial of red flags about ITS OWN tickers. A
  denial phrase counts only for the tickers its own words name (or that a company account maps to), or for the
  tracked red-flag post it replies to / quotes; when Claude scored the post, Claude must call it a denial or it
  must answer a tracked red flag. A denial that answers tracked posts denies exactly those; a stand-alone denial
  covers the ticker's unverified / likely-false red flags within ``red_flags.denial_lookback_hours`` (not
  confirmed or credible reports), not by the same author and not already denied. The denial is recorded too, so
  the same red flags are denied once.
* :meth:`claim_compromise`: at most one high-priority "may be hacked" alert per account per cooldown.

The tracker only stores and counts; the pipeline decides and alerts (see pipeline.py, alerts/decision.py).
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Iterable
from datetime import datetime, timedelta
from typing import Any

from .config import RedFlagSettings
from .models import Account, Credibility, Post, RedFlagSignal, Score
from .scoring.credibility import safe_handle
from .scoring.redflags import detect_denial
from .scoring.rules import own_text
from .store import Store
from .timeutil import now_utc, to_ms

logger = logging.getLogger(__name__)

KV_SPREAD_PREFIX = "red_flag_spread:"
KV_COMPROMISE_PREFIX = "red_flag_compromise:"
#: events are kept at least this long (and at least twice the denial lookback)
RETENTION = timedelta(days=30)
PRUNE_INTERVAL = timedelta(hours=1)
#: at most this many related posts are carried on a signal
MAX_RELATED = 20
#: claim statuses that make an official / trusted author a confirmation of the bad news
CONFIRMING_CLAIMS = frozenset({"confirmed_official", "credible_report"})
#: a stand-alone denial (not a reply / quote of a tracked post) never "denies" these
_NOT_DENIABLE_STANDALONE = CONFIRMING_CLAIMS


def _norm(t: Any) -> str:
    return str(t).strip().lstrip("$").upper()


class RedFlagTracker:
    """Per-ticker rolling window of red-flag posts, persisted in the store. See the module docstring."""

    def __init__(
        self,
        store: Store,
        settings: RedFlagSettings,
        my_tickers: Iterable[str],
        clock: Callable[[], datetime] = now_utc,
    ) -> None:
        self.store = store
        self.settings = settings
        self.my_tickers: tuple[str, ...] = tuple(dict.fromkeys(_norm(t) for t in my_tickers if _norm(t)))
        self._mine = frozenset(self.my_tickers)
        self.clock = clock
        self._last_prune: datetime | None = None

    # ------------------------------------------------------------------ helpers

    @property
    def enabled(self) -> bool:
        return bool(self.settings.enabled)

    @property
    def window(self) -> timedelta:
        return timedelta(minutes=int(self.settings.spread_window_minutes))

    @property
    def lookback(self) -> timedelta:
        return timedelta(hours=int(self.settings.denial_lookback_hours))

    def mine(self, tickers: Iterable[Any]) -> list[str]:
        """The tickers that are mine, normalized, in order, without duplicates."""
        return [t for t in dict.fromkeys(_norm(x) for x in tickers) if t in self._mine]

    def window_events(self, ticker: str, at: datetime) -> list[dict[str, Any]]:
        """Red-flag events for ``ticker`` posted within the spread window ending at ``at`` (oldest first)."""
        return self.store.red_flag_events(_norm(ticker), at - self.window, at)

    def events_for_posts(self, post_ids: Iterable[str]) -> list[dict[str, Any]]:
        return self.store.red_flag_events_for_posts(post_ids)

    def _signal(self, ticker: str, events: list[dict[str, Any]], minutes: int | None = None) -> RedFlagSignal:
        authors = {str(e["author_id"]) for e in events}
        confirmed: list[str] = []
        for e in events:
            confirming = e.get("claim_status") in CONFIRMING_CLAIMS
            if (e.get("official") or e.get("trusted")) and confirming and e.get("handle"):
                h = f"@{e['handle']}"
                if h not in confirmed:
                    confirmed.append(h)
        ids = list(dict.fromkeys(str(e["post_id"]) for e in events))[-MAX_RELATED:]
        return RedFlagSignal(
            ticker=ticker,
            distinct_authors=len(authors),
            window_minutes=int(self.settings.spread_window_minutes) if minutes is None else minutes,
            confirmed_by=confirmed,
            related_post_ids=ids,
        )

    def busiest_window(self, ticker: str, at: datetime) -> list[dict[str, Any]]:
        """Events of the window (``spread_window_minutes`` long) containing ``at`` with the most distinct authors.

        Events after ``at`` count too: posts finish scoring out of X-time order, and only the post with the latest
        X time would otherwise ever see the whole burst.
        """
        w = self.window
        events = self.store.red_flag_events(_norm(ticker), at - w, at + w)
        w_ms = int(w.total_seconds() * 1000)
        at_ms = to_ms(at)
        best: list[dict[str, Any]] = []
        best_n = -1
        hi = 0
        for lo, first in enumerate(events):
            start = int(first["created_at_ms"])
            if start > at_ms:
                break
            if start + w_ms < at_ms:
                continue
            hi = max(hi, lo)
            while hi < len(events) and int(events[hi]["created_at_ms"]) <= start + w_ms:
                hi += 1
            window = events[lo:hi]
            n = len({str(e["author_id"]) for e in window})
            if n > best_n:
                best, best_n = window, n
        return best

    def _cooling(self, key: str, now: datetime, minutes: int) -> bool:
        raw = self.store.kv_get(key)
        if not raw or minutes <= 0:
            return False
        try:
            last = int(raw)
        except ValueError:
            return False
        return last > 0 and to_ms(now) - last < minutes * 60_000

    def _maybe_prune(self, now: datetime) -> None:
        if self._last_prune is not None and now - self._last_prune < PRUNE_INTERVAL:
            return
        self._last_prune = now
        keep = max(RETENTION, 2 * self.lookback, 2 * self.window)
        try:
            removed = self.store.prune_red_flag_events(now - keep)
        except Exception:  # pruning is housekeeping; never break the alert path
            logger.debug("red flags: prune failed", exc_info=True)
            return
        if removed:
            logger.info("red flags: pruned %d old event(s)", removed)

    # ------------------------------------------------------------------ prompt hints

    def peek(self, tickers: Iterable[Any], at: datetime) -> RedFlagSignal | None:
        """What is known so far (before this post is scored): the ticker with the most bad-news authors in the
        window ending at ``at``, or None when there is nothing. A hint for Claude; records nothing."""
        if not self.enabled:
            return None
        best: RedFlagSignal | None = None
        for t in self.mine(tickers):
            sig = self._signal(t, self.window_events(t, at))
            if sig.distinct_authors and (best is None or sig.distinct_authors > best.distinct_authors):
                best = sig
        return best

    # ------------------------------------------------------------------ red flags / spreading

    def observe(
        self, post: Post, score: Score | None, credibility: Credibility | None, account: Account | None = None
    ) -> RedFlagSignal | None:
        """Record a red-flag post about my tickers and return its signal (``spreading`` claimed if due).

        None when the post is not a red flag about one of my tickers (or is a joke / a denial). ``account`` (the
        watched account that posted it, if any) decides for which tickers the author counts as official.
        """
        if not self.enabled or score is None or not score.red_flag or score.is_joke:
            return None
        if score.claim_status == "denial":
            return None
        tickers = self.mine(score.tickers)
        if not tickers:
            return None
        now = self.clock()
        cred = credibility
        own = {_norm(t) for t in account.tickers} if account is not None else set()
        is_official = bool(cred is not None and cred.is_official and not cred.possible_compromise)
        trusted = bool(cred is not None and cred.level == "trusted")
        for t in tickers:
            self.store.record_red_flag_event(
                kind="red_flag",
                ticker=t,
                post_id=post.id,
                author_id=post.author_id,
                handle=safe_handle(post.author_username),
                created_at=post.created_at,
                credibility=cred.level if cred is not None else None,
                source_kind=cred.source_kind if cred is not None else None,
                types=score.red_flag_types,
                claim_status=score.claim_status,
                impact=int(score.impact),
                official=is_official and t in own,  # a CEO is official about its own company only
                trusted=trusted,
                recorded_at=now,
            )
        min_authors = int(self.settings.spread_min_authors)
        cooldown = int(self.settings.spread_cooldown_minutes)
        best: tuple[tuple[bool, int], RedFlagSignal] | None = None
        for t in tickers:
            sig = self._signal(t, self.busiest_window(t, post.created_at))
            due = sig.distinct_authors >= min_authors and not self._cooling(KV_SPREAD_PREFIX + t, now, cooldown)
            key = (due, sig.distinct_authors)
            if best is None or key > best[0]:
                best = (key, sig)
        self._maybe_prune(now)
        assert best is not None
        (due, _), signal = best
        if due:
            signal.spreading = True
            self.store.kv_set(KV_SPREAD_PREFIX + signal.ticker, str(to_ms(now)))
            logger.info(
                "red flags: $%s spreading - %d accounts within %d min (post %s)",
                signal.ticker,
                signal.distinct_authors,
                signal.window_minutes,
                post.id,
            )
        return signal

    def release_spread(self, ticker: str) -> None:
        """Hand back a claimed spreading cooldown (the alert could not be delivered): the next post retries."""
        self.store.kv_set(KV_SPREAD_PREFIX + _norm(ticker), "0")

    # ------------------------------------------------------------------ denials

    def denial_tickers(
        self,
        post: Post,
        account: Account | None,
        score: Score | None,
        own_tickers: Iterable[str] = (),
    ) -> list[str]:
        """My tickers a watched account's post denies bad news about, or [] when it is not a denial.

        A denial needs a denial phrase or Claude's claim_status "denial", and is about the account's OWN tickers
        only (a CEO saying "fake news" about another company is not that company's denial). The tickers come from:
        the tracked red-flag post it replies to / quotes; the tickers Claude names in a denial; or, for a phrase in a
        post Claude did not score, the tickers named in the post's OWN words (``own_tickers``, never the parent
        post's) - a company account's phrase also covers its own tickers. When Claude scored the post, a phrase
        alone is not enough: Claude must call it a denial, or it must answer a tracked red flag. A bare "Fake news"
        reply to an unrelated post that merely names Tesla denies nothing.
        """
        if not self.enabled or account is None:
            return []
        if score is not None and score.red_flag:
            return []  # it reports bad news itself (perhaps denying something else): never a "DENIED" alert
        phrases = detect_denial(own_text(post))
        # (a rules-only score's "denial" comes from the same phrases, with tickers guessed from the whole thread:
        # only Claude's own reading counts as a denial that names tickers)
        claude_scored = score is not None and score.source == "claude"
        claude_denial = claude_scored and score is not None and score.claim_status == "denial"
        if not phrases and not claude_denial:
            return []
        found: list[str] = [e["ticker"] for e in self.events_for_posts(self.referenced_ids(post))]
        if claude_denial and score is not None:
            found.extend(score.tickers)
        if phrases and not claude_scored:
            found.extend(own_tickers)
            if account.kind == "company":
                found.extend(account.tickers)
        own = {_norm(t) for t in account.tickers}
        return [t for t in self.mine(found) if t in own]

    @staticmethod
    def referenced_ids(post: Post) -> list[str]:
        return [r.id for r in post.referenced if r.type in ("replied_to", "quoted")]

    def find_denial(self, post: Post, tickers: Iterable[str]) -> RedFlagSignal | None:
        """Red flags (not by this author, not yet denied) about ``tickers`` that this denial answers.

        A reply to / quote of tracked red-flag posts denies exactly those posts. A stand-alone denial covers the
        ticker's red flags in the lookback before it, except confirmed / credible reports (a CEO's "fake news" does
        not make a regulator's filing false). Records the denial so the same red flags are not denied twice. None
        when there is nothing to deny.
        """
        if not self.enabled:
            return None
        since = post.created_at - self.lookback
        handle = safe_handle(post.author_username)
        hours = int(self.settings.denial_lookback_hours)
        wanted = self.mine(tickers)
        refs = set(self.referenced_ids(post))
        targeted = [e for e in self.events_for_posts(refs) if e["ticker"] in wanted] if refs else []
        found: list[tuple[str, list[dict[str, Any]]]] = []
        for t in wanted:
            denied = self.store.denied_red_flag_post_ids(t, since)
            if targeted:
                candidates = [e for e in targeted if e["ticker"] == t]
            else:
                candidates = [
                    e
                    for e in self.store.red_flag_events(t, since, post.created_at)
                    if e.get("claim_status") not in _NOT_DENIABLE_STANDALONE
                ]
            events = [
                e
                for e in candidates
                if str(e["author_id"]) != post.author_id and str(e["post_id"]) not in denied and e["post_id"] != post.id
            ]
            if events:
                found.append((t, events))
        if not found:
            return None
        now = self.clock()
        for t, events in found:
            self.store.record_red_flag_event(
                kind="denial",
                ticker=t,
                post_id=post.id,
                author_id=post.author_id,
                handle=handle,
                created_at=post.created_at,
                credibility="official",
                related=[str(e["post_id"]) for e in events],
                official=True,
                recorded_at=now,
            )
        ticker, events = max(found, key=lambda item: len(item[1]))
        signal = self._signal(ticker, events, minutes=hours * 60)
        signal.confirmed_by = []  # the denied posts confirm nothing here
        signal.denied_by = [f"@{handle}"] if handle else []
        logger.info(
            "red flags: post %s by @%s denies %d recent red flag(s) about $%s",
            post.id,
            handle or post.author_id,
            len(signal.related_post_ids),
            ticker,
        )
        return signal

    # ------------------------------------------------------------------ hijacked accounts

    def claim_compromise(self, author_id: str) -> bool:
        """True for the first "may be hacked" alert of an account within the cooldown (claims it)."""
        now = self.clock()
        key = KV_COMPROMISE_PREFIX + str(author_id)
        if self._cooling(key, now, int(self.settings.spread_cooldown_minutes)):
            return False
        self.store.kv_set(key, str(to_ms(now)))
        return True


__all__ = ["KV_COMPROMISE_PREFIX", "KV_SPREAD_PREFIX", "RedFlagTracker"]
