"""The post pipeline: parse -> dedupe -> store -> fast rules -> Claude -> decide -> alert.

Every ingest path (filtered stream, timeline poller, gap recovery, replay) puts stream-shaped
payloads on one queue as :class:`~ceoscan.models.IngestItem`; :class:`Pipeline` workers take them
off and run each through the same steps:

1. ``payload_to_post`` normalizes the JSON into a :class:`~ceoscan.models.Post` (malformed -> dropped).
2. Dedupe by post id (``Store.insert_post_if_new``: in-memory LRU + SQLite primary key). Every new post
   is stored before anything else happens, so the DB holds every post we ever saw.
3. Fast regex rules; flagged posts (and always-alert accounts) go to the scorer (Claude) together with
   the quoted / parent post text carried on ``Post.referenced``.
4. ``decide`` -> format -> dispatch to the phone, with the latency of every step measured and stored
   on the ``alerts`` row (target: under 10 s from post creation to phone).

Post text is untrusted. It is only stored, passed to the scorer as data, and displayed.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Literal, Protocol

from .alerts.decision import decide
from .alerts.dispatcher import AlertDispatcher
from .alerts.format import format_post_alert
from .config import TICKER_RE, AppConfig
from .models import (
    Account,
    AlertRecord,
    ChannelResult,
    Decision,
    IngestItem,
    IngestStatus,
    Post,
    ReferencedPost,
    RuleResult,
    Score,
    Watchlist,
)
from .scoring.claude import fallback_score
from .scoring.rules import FastRules
from .store import Store
from .timeutil import now_utc, snowflake_to_datetime

logger = logging.getLogger(__name__)

#: user requirement: post -> phone in under 10 seconds
LATENCY_TARGET_SECONDS = 10.0

_REFERENCE_TYPES = ("quoted", "replied_to", "retweeted")
_MAX_ERROR_DETAIL = 300


class Scorer(Protocol):
    """What the pipeline needs from ClaudeScorer / OfflineScorer."""

    async def score(
        self,
        post: Post,
        account: Account | None,
        rules: RuleResult,
        my_tickers: tuple[str, ...],
        tz: str,
    ) -> Score: ...


# --------------------------------------------------------------------------- parsing


def _id_str(value: Any) -> str | None:
    """A numeric X id as a string, or None. Accepts "123" and 123 (never bools/negatives)."""
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return str(value) if value > 0 else None
    if isinstance(value, str):
        v = value.strip()
        if v and v.isascii() and v.isdigit() and int(v) > 0:
            return v
    return None


def _opt_str(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    return value.strip() or None


def _dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _list(value: Any) -> list[Any]:
    return value if isinstance(value, list) else []


def _index_by_id(items: Any) -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    for obj in _list(items):
        if isinstance(obj, dict) and (oid := _id_str(obj.get("id"))) is not None:
            out.setdefault(oid, obj)
    return out


def _full_text(obj: dict[str, Any]) -> str | None:
    """note_tweet.text for long posts (data.text is truncated there), else text."""
    note = _dict(obj.get("note_tweet"))
    note_text = note.get("text")
    if isinstance(note_text, str) and note_text.strip():
        return note_text
    text = obj.get("text")
    return text if isinstance(text, str) else None


def _entity_lists(obj: dict[str, Any]) -> list[dict[str, Any]]:
    """data.entities plus note_tweet.entities (which covers the part beyond the truncation)."""
    return [e for e in (_dict(obj.get("entities")), _dict(_dict(obj.get("note_tweet")).get("entities"))) if e]


def _dedupe(items: Iterable[str]) -> list[str]:
    return list(dict.fromkeys(i for i in items if i))


def _cashtags(entity_sets: list[dict[str, Any]]) -> list[str]:
    tags: list[str] = []
    for ents in entity_sets:
        for c in _list(ents.get("cashtags")):
            tag = _opt_str(_dict(c).get("tag"))
            if tag:
                t = tag.lstrip("$").upper()
                if TICKER_RE.match(t):
                    tags.append(t)
    return _dedupe(tags)


def _mentions(entity_sets: list[dict[str, Any]]) -> list[str]:
    names: list[str] = []
    for ents in entity_sets:
        for m in _list(ents.get("mentions")):
            name = _opt_str(_dict(m).get("username"))
            if name:
                names.append(name.lstrip("@"))
    return _dedupe(names)


def _urls(entity_sets: list[dict[str, Any]]) -> list[str]:
    urls: list[str] = []
    for ents in entity_sets:
        for u in _list(ents.get("urls")):
            d = _dict(u)
            url = _opt_str(d.get("expanded_url")) or _opt_str(d.get("url"))
            if url:
                urls.append(url)
    return _dedupe(urls)


def _followers(user: dict[str, Any]) -> int | None:
    n = _dict(user.get("public_metrics")).get("followers_count")
    return n if isinstance(n, int) and not isinstance(n, bool) else None


def _created_at(post_id: str, data: dict[str, Any]) -> datetime:
    """Creation time from the snowflake id (ms precision); data.created_at only for pre-snowflake ids."""
    if int(post_id) >> 22 == 0:
        raw = data.get("created_at")
        if isinstance(raw, str):
            try:
                dt = datetime.fromisoformat(raw)  # 3.11+: accepts the trailing "Z"
                return dt if dt.tzinfo else dt.replace(tzinfo=UTC)
            except ValueError:
                pass
    return snowflake_to_datetime(post_id)


def _utc(dt: datetime) -> datetime:
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=UTC)


def payload_to_post(payload: dict, received_at: datetime, source: str, watchlist: Watchlist) -> Post | None:
    """Normalize one stream-shaped payload ``{"data", "includes", "matching_rules"}`` into a Post.

    Returns None for malformed payloads (no data object, non-numeric id, missing author or text).
    Missing usernames are filled from the watchlist by user id.
    """
    try:
        return _payload_to_post(payload, received_at, source, watchlist)
    except (TypeError, ValueError, AttributeError, KeyError, OverflowError) as exc:
        logger.debug("payload_to_post: malformed payload (%s)", type(exc).__name__)
        return None


def _payload_to_post(payload: Any, received_at: datetime, source: str, watchlist: Watchlist) -> Post | None:
    if not isinstance(payload, dict):
        return None
    data = payload.get("data")
    if not isinstance(data, dict):
        return None
    post_id = _id_str(data.get("id"))
    author_id = _id_str(data.get("author_id"))
    text = _full_text(data)
    if post_id is None or author_id is None or text is None:
        return None

    includes = _dict(payload.get("includes"))
    users = _index_by_id(includes.get("users"))
    tweets = _index_by_id(includes.get("tweets"))
    watched = watchlist.by_user_id()

    def username_for(uid: str | None) -> str | None:
        if uid is None:
            return None
        name = _opt_str(users.get(uid, {}).get("username"))
        if name:
            return name.lstrip("@")
        acct = watched.get(uid)
        return acct.handle if acct else None

    author = users.get(author_id, {})

    referenced: list[ReferencedPost] = []
    seen_refs: set[tuple[str, str]] = set()
    for ref in _list(data.get("referenced_tweets")):
        ref = _dict(ref)
        rtype, rid = ref.get("type"), _id_str(ref.get("id"))
        if rtype not in _REFERENCE_TYPES or rid is None or (rtype, rid) in seen_refs:
            continue
        seen_refs.add((rtype, rid))
        ref_obj = tweets.get(rid, {})
        ref_author = _id_str(ref_obj.get("author_id"))
        referenced.append(
            ReferencedPost(
                type=rtype,
                id=rid,
                text=_full_text(ref_obj) if ref_obj else None,
                author_id=ref_author,
                author_username=username_for(ref_author),
            )
        )

    entity_sets = _entity_lists(data)
    lang = data.get("lang")
    return Post(
        id=post_id,
        text=text,
        author_id=author_id,
        created_at=_created_at(post_id, data),
        received_at=_utc(received_at),
        source=source,  # type: ignore[arg-type]
        author_username=username_for(author_id),
        author_name=_opt_str(author.get("name")),
        author_followers=_followers(author),
        author_verified=author.get("verified") if isinstance(author.get("verified"), bool) else None,
        conversation_id=_id_str(data.get("conversation_id")),
        in_reply_to_user_id=_id_str(data.get("in_reply_to_user_id")),
        lang=lang if isinstance(lang, str) and lang else None,
        referenced=referenced,
        cashtags=_cashtags(entity_sets),
        mentions=_mentions(entity_sets),
        urls=_urls(entity_sets),
        matching_rule_tags=_dedupe(
            t for r in _list(payload.get("matching_rules")) if (t := _opt_str(_dict(r).get("tag")))
        ),
        edit_history_ids=_dedupe(
            i for i in (_id_str(x) for x in _list(data.get("edit_history_tweet_ids"))) if i
        ),
        raw=payload,
    )


# --------------------------------------------------------------------------- pipeline

Outcome = Literal["malformed", "duplicate", "no_alert", "edit_skipped", "alert", "error"]


@dataclass
class HandleResult:
    """Everything the pipeline learned about one ingest item (used by replay and tests)."""

    outcome: Outcome
    post: Post | None = None
    account: Account | None = None
    rules: RuleResult | None = None
    score: Score | None = None
    decision: Decision | None = None
    alert: AlertRecord | None = None
    detail: str = ""


def _ms(seconds: float) -> int:
    return round(seconds * 1000)


def _fmt_s(ms: int | None) -> str:
    return "n/a" if ms is None else f"{ms / 1000:.2f}s"


def _first_success_ms(results: list[ChannelResult]) -> int | None:
    """Dispatch -> first successful channel, in ms (results carry elapsed-from-dispatch-start)."""
    ok = [r.elapsed_ms for r in results if r.ok]
    return min(ok) if ok else None


class Pipeline:
    """Runs ingest items through rules, scoring, decision and alerting. See the module docstring."""

    #: how often an idle worker re-checks the stop event; has no effect on alert latency
    idle_poll_seconds: float = 0.5

    def __init__(
        self,
        cfg: AppConfig,
        store: Store,
        scorer: Scorer,
        dispatcher: AlertDispatcher,
        status: IngestStatus,
        fast_rules: FastRules | None = None,
        clock: Callable[[], datetime] = now_utc,
    ) -> None:
        self.cfg = cfg
        self.settings = cfg.settings
        self.watchlist = cfg.watchlist
        self.store = store
        self.scorer = scorer
        self.dispatcher = dispatcher
        self.status = status
        self.fast_rules = fast_rules if fast_rules is not None else FastRules(cfg.watchlist)
        self.clock = clock
        self.my_tickers: tuple[str, ...] = tuple(cfg.watchlist.my_tickers)
        self.tz = cfg.settings.alerts.timezone
        self._by_id = cfg.watchlist.by_user_id()
        self._by_handle = cfg.watchlist.by_handle()

    # ------------------------------------------------------------------ helpers

    def find_account(self, post: Post) -> Account | None:
        """Watchlist account for the author: by user id, else by username (case-insensitive).

        A username match is refused when the watchlist pins a *different* user id for that handle:
        that is another account now using the handle (rename / look-alike), not the one we watch.
        """
        acct = self._by_id.get(post.author_id)
        if acct is not None:
            return acct
        if not post.author_username:
            return None
        acct = self._by_handle.get(post.author_username.lower())
        if acct is None:
            return None
        if acct.user_id is not None and acct.user_id != post.author_id:
            logger.warning(
                "post %s: @%s has author id %s but the watchlist pins user id %s - not treated as watched",
                post.id,
                post.author_username,
                post.author_id,
                acct.user_id,
            )
            return None
        return acct

    def _earlier_alert_score(self, post: Post, impact: int) -> tuple[str, int | None] | None:
        """(earlier id, its score) if an earlier version of this edited post already alerted >= impact."""
        for eid in post.edit_history_ids:
            if eid == post.id or not self.store.alert_exists_for_post(eid):
                continue
            row = self.store.get_post_row(eid)
            prev = row.get("score") if row else None
            if prev is None or int(prev) >= impact:
                return eid, prev
        return None

    def _handle_name(self, post: Post, account: Account | None) -> str:
        if post.author_username:
            return f"@{post.author_username}"
        if account is not None:
            return f"@{account.handle}"
        return f"user {post.author_id}"

    # ------------------------------------------------------------------ main entry points

    async def handle(self, item: IngestItem) -> AlertRecord | None:
        """Process one item; returns the AlertRecord if an alert was sent (or attempted)."""
        return (await self.process(item)).alert

    async def process(self, item: IngestItem) -> HandleResult:
        """Like :meth:`handle` but returns the full :class:`HandleResult`. Never raises (except cancel)."""
        result = HandleResult(outcome="error")
        try:
            await self._process(item, result)
        except Exception as exc:
            result.outcome = "error"
            result.detail = " ".join(f"{type(exc).__name__}: {exc}".split())[:_MAX_ERROR_DETAIL]
            post_id = result.post.id if result.post is not None else "?"
            logger.exception("pipeline error for post %s", post_id)
            try:
                self.store.log_event("pipeline_error", f"post {post_id}: {result.detail}", level="error")
            except Exception:  # the store itself may be what failed
                logger.exception("could not record pipeline_error event")
        return result

    async def _process(self, item: IngestItem, result: HandleResult) -> None:
        sc = self.settings.scoring

        # 1. parse
        post = payload_to_post(item.payload, item.received_at, item.source, self.watchlist)
        if post is None:
            result.outcome = "malformed"
            keys = sorted(str(k) for k in item.payload)[:10] if isinstance(item.payload, dict) else []
            result.detail = f"malformed {item.source} payload (keys: {', '.join(keys) or 'none'})"
            logger.warning("dropping %s", result.detail)
            self.store.log_event("malformed_payload", result.detail, level="warning")
            return
        result.post = post

        # 2. dedupe (and store every post)
        if not self.store.insert_post_if_new(post):
            result.outcome = "duplicate"
            result.detail = f"duplicate post id {post.id} ignored"
            logger.debug("duplicate post %s (source=%s) ignored", post.id, post.source)
            return

        # 3. status + since_id ; 4. account
        self.status.last_post_at = post.received_at
        self.status.last_post_id = post.id
        account = self.find_account(post)
        result.account = account
        if account is not None and post.source != "replay":  # replayed samples are not real X posts
            self.store.set_since_id(post.author_id, post.id)

        # 5. fast rules
        rules = self.fast_rules.evaluate(post, account)
        result.rules = rules

        # 6. score: flagged posts, and always-alert accounts (Claude only if configured to)
        score: Score | None = None
        always = account is not None and account.always_alert
        if rules.flagged or (always and sc.score_always_alert_unflagged):
            score = await self.scorer.score(post, account, rules, self.my_tickers, self.tz)
        elif always:
            # Not worth a Claude call per settings, but always-alert accounts still alert on every post.
            score = fallback_score(rules, account, None, source="skipped")
        result.score = score
        t_scored = self.clock()

        # 7. decide ; 8. store the score
        decision = decide(score, rules, account, self.settings, t_scored, self.my_tickers)
        result.decision = decision
        self.store.update_post_score(post.id, rules, score)

        handle = self._handle_name(post, account)
        if not decision.should_alert or score is None:
            result.outcome = "no_alert"
            result.detail = decision.reason
            log = logger.info if rules.flagged else logger.debug
            log(
                "post %s %s flagged=%s score=%s: no alert (%s)",
                post.id,
                handle,
                rules.flagged,
                score.impact if score else "-",
                decision.reason,
            )
            return

        # 9. alert (unless an earlier version of this edited post already alerted at >= this score)
        earlier = self._earlier_alert_score(post, score.impact)
        if earlier is not None:
            eid, prev = earlier
            result.outcome = "edit_skipped"
            result.detail = f"edit of post {eid} which already alerted (score {prev if prev is not None else '?'})"
            logger.info("post %s %s: %s; not alerting again", post.id, handle, result.detail)
            return

        msg = format_post_alert(post, account, score, decision, self.settings.alerts)
        t_push = self.clock()
        results = await self.dispatcher.send(msg, score.impact)

        push_ms = _first_success_ms(results)
        x_delay_ms = _ms((post.received_at - post.created_at).total_seconds())
        scoring_ms = _ms((t_scored - post.received_at).total_seconds())
        total_ms = (
            _ms((t_push - post.created_at).total_seconds()) + push_ms if push_ms is not None else None
        )
        rec = AlertRecord(
            post_id=post.id,
            kind="post",
            decision_reason=decision.reason,
            message=msg,
            results=results,
            score=score.impact,
            direction=score.direction,
            tickers=list(score.tickers),
            quiet_hours=decision.quiet_hours,
            total_latency_ms=total_ms,
            x_delay_ms=x_delay_ms,
            scoring_ms=scoring_ms,
            push_ms=push_ms,
        )
        result.alert = rec
        result.outcome = "alert"
        result.detail = decision.reason
        self.store.record_alert(rec)
        self._log_alert(rec, handle)

    def _log_alert(self, rec: AlertRecord, handle: str) -> None:
        tickers = ",".join(rec.tickers) or "-"
        latency = (
            f"latency total={_fmt_s(rec.total_latency_ms)} x={_fmt_s(rec.x_delay_ms)} "
            f"scoring={_fmt_s(rec.scoring_ms)} push={_fmt_s(rec.push_ms)}"
        )
        if not rec.sent_ok:
            errors = "; ".join(f"{r.channel}: {r.error or 'failed'}" for r in rec.results) or "no channels"
            logger.error(
                "ALERT NOT DELIVERED %s %s %s/10 %s post=%s %s (%s)",
                handle,
                tickers,
                rec.score,
                rec.direction,
                rec.post_id,
                latency,
                errors,
            )
            self.store.log_event("alert_failed", f"post {rec.post_id}: {errors}", level="error")
            return
        logger.info("ALERT %s %s %s/10 %s %s", handle, tickers, rec.score, rec.direction, latency)
        if rec.total_latency_ms is not None and rec.total_latency_ms > LATENCY_TARGET_SECONDS * 1000:
            logger.warning(
                "alert latency %s for post %s exceeds the %.0f s target (x=%s scoring=%s push=%s)",
                _fmt_s(rec.total_latency_ms),
                rec.post_id,
                LATENCY_TARGET_SECONDS,
                _fmt_s(rec.x_delay_ms),
                _fmt_s(rec.scoring_ms),
                _fmt_s(rec.push_ms),
            )

    # ------------------------------------------------------------------ workers

    async def run(self, queue: asyncio.Queue, stop: asyncio.Event, workers: int = 4) -> None:
        """Process queue items with ``workers`` concurrent workers until ``stop`` is set.

        A worker finishes the item it is on before exiting; items still queued at stop are left.
        """
        n = max(1, int(workers))
        tasks = [asyncio.create_task(self._worker(queue, stop), name=f"pipeline-worker-{i}") for i in range(n)]
        logger.info("pipeline started with %d worker(s)", n)
        try:
            await asyncio.gather(*tasks)
        finally:
            for t in tasks:
                if not t.done():
                    t.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            if queue.qsize():
                logger.info("pipeline stopped with %d item(s) still queued", queue.qsize())
            else:
                logger.info("pipeline stopped")

    async def _worker(self, queue: asyncio.Queue, stop: asyncio.Event) -> None:
        while not stop.is_set():
            try:
                item = await asyncio.wait_for(queue.get(), timeout=self.idle_poll_seconds)
            except TimeoutError:
                continue
            try:
                await self.process(item)
            finally:
                queue.task_done()


__all__ = [
    "LATENCY_TARGET_SECONDS",
    "HandleResult",
    "Pipeline",
    "Scorer",
    "payload_to_post",
]
