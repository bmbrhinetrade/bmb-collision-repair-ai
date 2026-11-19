"""The post pipeline: parse -> dedupe -> store -> fast rules -> Claude -> decide -> alert.

Every ingest path (filtered stream, timeline poller, gap recovery, replay) puts stream-shaped
payloads on one queue as :class:`~ceoscan.models.IngestItem`; :class:`Pipeline` workers take them
off and run each through the same steps:

1. ``payload_to_post`` normalizes the JSON into a :class:`~ceoscan.models.Post` (malformed -> dropped).
2. Dedupe by post id (``Store.insert_post_if_new``: in-memory LRU + SQLite primary key). Every new post
   is stored before anything else happens, so the DB holds every post we ever saw.
3. Fast regex rules. What goes to the scorer (Claude), together with the quoted / parent post text
   carried on ``Post.referenced`` and the rule results as hints:

   * every post from a watched account when ``scoring.score_watchlist_unflagged`` (the default; only
     content-free posts - no letters or digits and no referenced post, link or media - are skipped, and
     replies / reposts only when the account includes them);
   * always-alert accounts per ``scoring.score_always_alert_unflagged``;
   * everything else (keyword-rule posts from non-watched accounts) only when the fast rules flag it.

4. ``decide`` -> format -> dispatch to the phone, with the latency of every step measured and stored
   on the ``alerts`` row (target: under 10 s from post creation to phone).
5. Optional: once the alert is recorded, strong alerts are handed to the Claude analyst desk
   (``desk.submit``, alerts/claude_desk.py). That call only queues the alert; the push never waits for it.

Late posts (``received_at - created_at > alerts.max_post_age_seconds``: outage / restart catch-up) are
stored and scored like any other, but alert at most at "normal" priority with a "Missed during outage"
title, never by SMS, and their alerts are flagged ``late`` so they stay out of the latency stats.

An edit of a post that already alerted alerts again only when it changes the picture (direction flips,
impact rises by >= 2, or a watched ticker appears), marked as edited.

On stop, workers finish what is already queued for up to ``drain_seconds``; anything still queued
after that is stored (never silently dropped) but not scored or alerted.

Post text is untrusted. It is only stored, passed to the scorer as data, and displayed.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, Literal, Protocol

from .alerts.decision import decide, format_age, is_late, post_age_seconds
from .alerts.dispatcher import AlertDispatcher
from .alerts.format import RULES_ONLY_SOURCES, format_post_alert
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
from .store import CATCH_UP_SOURCE, Store
from .timeutil import now_utc, snowflake_to_datetime

if TYPE_CHECKING:  # pragma: no cover - typing only
    from .alerts.claude_desk import ClaudeDesk

logger = logging.getLogger(__name__)

#: user requirement: post -> phone in under 10 seconds
LATENCY_TARGET_SECONDS = 10.0

_REFERENCE_TYPES = ("quoted", "replied_to", "retweeted")
_MAX_ERROR_DETAIL = 300
#: an edit re-alerts when its impact is at least this much above the alert the user already got
EDIT_REALERT_MIN_RISE = 2
_DIRECTIONAL = ("bullish", "bearish")


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


# --------------------------------------------------------------------------- scoring gate


def _has_media(post: Post) -> bool:
    data = _dict(_dict(post.raw).get("data"))
    attachments = _dict(data.get("attachments"))
    return bool(_list(attachments.get("media_keys")) or _list(attachments.get("poll_ids")))


def has_content(post: Post) -> bool:
    """False only for content-free posts: no letter or digit, no referenced post, no link and no media.

    Deliberately not a word count: "Funding secured." is two words and market moving.
    """
    if any(ch.isalnum() for ch in post.text or ""):
        return True
    return bool(post.referenced or post.urls or _has_media(post))


def is_repost(post: Post) -> bool:
    return any(r.type == "retweeted" for r in post.referenced)


ScoreRoute = Literal["scorer", "skipped", "none"]


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
    #: on stop, keep fully processing already-queued items for this long (app shutdown budget is 15 s;
    #: an item started before the deadline may take one Claude budget + one send timeout to finish)
    drain_seconds: float = 5.0

    def __init__(
        self,
        cfg: AppConfig,
        store: Store,
        scorer: Scorer,
        dispatcher: AlertDispatcher,
        status: IngestStatus,
        fast_rules: FastRules | None = None,
        clock: Callable[[], datetime] = now_utc,
        desk: ClaudeDesk | None = None,
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
        #: Claude analyst desk (None = off). Only ever gets a non-blocking submit after the alert is recorded.
        self.desk = desk
        self.my_tickers: tuple[str, ...] = tuple(cfg.watchlist.my_tickers)
        self.tz = cfg.settings.alerts.timezone
        self._by_id = cfg.watchlist.by_user_id()
        self._by_handle = cfg.watchlist.by_handle()
        self._my_tickers_upper = frozenset(t.upper() for t in self.my_tickers)
        self._drain_deadline: float | None = None

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

    def score_route(self, post: Post, account: Account | None, rules: RuleResult) -> ScoreRoute:
        """Where a post's score comes from: "scorer" (Claude), "skipped" (rules-only) or "none" (unscored)."""
        sc = self.settings.scoring
        if rules.flagged:
            return "scorer"
        if account is None:
            return "none"  # keyword-rule posts from non-watched accounts stay gated by the fast rules
        if account.always_alert:
            # always-alert accounts alert on every post, scored by Claude only if configured to
            return "scorer" if sc.score_always_alert_unflagged else "skipped"
        if sc.score_watchlist_unflagged and self._worth_scoring(post, account):
            return "scorer"  # "Funding secured." / a CEO resignation slip past keyword rules
        return "none"

    @staticmethod
    def _worth_scoring(post: Post, account: Account) -> bool:
        if post.is_reply and not account.include_replies:
            return False
        if is_repost(post) and not account.include_retweets:
            return False
        return has_content(post)

    def _cap_rules_only(self, score: Score) -> None:
        """Rules-only scores never exceed scoring.fallback_max_impact (no urgent / SMS on keywords alone).

        The scorers apply the cap too; doing it here as well keeps the guarantee for every source,
        including the pipeline's own "skipped" scores.
        """
        if score.source in RULES_ONLY_SOURCES:
            score.impact = min(int(score.impact), max(1, int(self.settings.scoring.fallback_max_impact)))

    def _earlier_alert(self, post: Post) -> tuple[str, dict[str, Any]] | None:
        """(id, alert) for the newest earlier version of this edited post that already alerted."""
        earlier = {e for e in post.edit_history_ids if e != post.id}
        for eid in sorted(earlier, key=int, reverse=True):
            row = self.store.last_alert_for_post(eid)
            if row is not None:
                return eid, row
        return None

    def _edit_change(self, prev: dict[str, Any], score: Score) -> str | None:
        """What makes this edit worth a new alert compared with the alert already sent, or None.

        Only a Claude score can re-alert an edit: a rules-only fallback (outage) has no direction and
        guessed tickers, so it would "change" every earlier Claude alert. Changes to or from "unclear"
        do not count either.
        """
        if score.source != "claude":
            return None
        changes: list[str] = []
        before, after = prev.get("direction"), score.direction
        if before in _DIRECTIONAL and after in _DIRECTIONAL and before != after:
            changes.append(f"was {str(before).upper()}, now {after.upper()}")
        prev_score = prev.get("score")
        if prev_score is not None and int(score.impact) - int(prev_score) >= EDIT_REALERT_MIN_RISE:
            changes.append(f"score was {int(prev_score)}/10, now {int(score.impact)}/10")
        prev_tickers = {str(t).strip().lstrip("$").upper() for t in prev.get("tickers") or []}
        added = [
            t
            for t in dict.fromkeys(str(x).strip().lstrip("$").upper() for x in score.tickers)
            if t in self._my_tickers_upper and t not in prev_tickers
        ]
        if added:
            changes.append(f"now also names {', '.join(added)}")
        return "; ".join(changes) or None

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

        # 6. score: see score_route (watched accounts, always-alert accounts, flagged keyword posts)
        score: Score | None = None
        route = self.score_route(post, account, rules)
        if route == "scorer":
            score = await self.scorer.score(post, account, rules, self.my_tickers, self.tz)
        elif route == "skipped":
            # Not worth a Claude call per settings, but always-alert accounts still alert on every post.
            score = fallback_score(
                rules, account, None, source="skipped", max_impact=self.settings.scoring.fallback_max_impact
            )
        if score is not None:
            self._cap_rules_only(score)
        result.score = score
        t_scored = self.clock()

        # 7. decide ; 8. store the score
        age = post_age_seconds(post)
        late = is_late(age, self.settings.alerts)
        decision = decide(score, rules, account, self.settings, t_scored, self.my_tickers, age_seconds=age)
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

        # 9. alert (an edit of a post that already alerted only when it changes the picture)
        edit_note: str | None = None
        earlier = self._earlier_alert(post)
        if earlier is not None:
            eid, prev = earlier
            edit_note = self._edit_change(prev, score)
            if edit_note is None:
                prev_score = prev.get("score")
                result.outcome = "edit_skipped"
                result.detail = (
                    f"edit of post {eid} which already alerted "
                    f"(score {prev_score if prev_score is not None else '?'}, {prev.get('direction') or '?'}); "
                    "no material change"
                )
                logger.info("post %s %s: %s; not alerting again", post.id, handle, result.detail)
                return
            logger.info("post %s %s: edit of alerted post %s re-alerts: %s", post.id, handle, eid, edit_note)

        desk_brief = self.desk is not None and self.desk.wants(score, account, late)
        msg = format_post_alert(
            post, account, score, decision, self.settings.alerts, edit_note=edit_note, desk_brief=desk_brief
        )
        t_push = self.clock()
        # Late posts carry the "late" tag (format_post_alert): the dispatcher never sends them by SMS and
        # Pushover never gives them priority 1, whatever the score; email (score >= email.min_score) still
        # goes out, and the message priority is at most "normal".
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
            late=late,
            total_latency_ms=total_ms,
            x_delay_ms=x_delay_ms,
            scoring_ms=scoring_ms,
            push_ms=push_ms,
        )
        result.alert = rec
        result.outcome = "alert"
        result.detail = decision.reason
        self.store.record_alert(rec)
        if self.desk is not None:  # queues only (never blocks or raises); the push already went out
            self.desk.submit(rec, post, score, account, rules)
        self._log_alert(rec, handle, catch_up=post.source == CATCH_UP_SOURCE)

    def _log_alert(self, rec: AlertRecord, handle: str, catch_up: bool = False) -> None:
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
        if rec.late:
            age = format_age((rec.x_delay_ms or 0) / 1000)
            logger.info(
                "LATE ALERT %s %s %s/10 %s (posted %s before we received it; not counted in latency stats) %s",
                handle,
                tickers,
                rec.score,
                rec.direction,
                age,
                latency,
            )
            return
        logger.info(
            "ALERT %s %s %s/10 %s %s%s",
            handle,
            tickers,
            rec.score,
            rec.direction,
            latency,
            " (catch-up; not counted in latency stats)" if catch_up else "",
        )
        if catch_up:
            return  # fetched by gap / restart catch-up: its latency is the outage, not the live path
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

        On stop each worker finishes the item it is on, then keeps taking already-queued items for up to
        ``drain_seconds`` (full processing: score + alert). Whatever is still queued after that, or when
        the pipeline is cancelled, is stored without scoring or alerting - ingest has already advanced
        its since_id cursor past those posts, so dropping them would lose them for good.
        """
        n = max(1, int(workers))
        self._drain_deadline = None
        tasks = [asyncio.create_task(self._worker(queue, stop), name=f"pipeline-worker-{i}") for i in range(n)]
        logger.info("pipeline started with %d worker(s)", n)
        try:
            await asyncio.gather(*tasks)
        finally:
            for t in tasks:
                if not t.done():
                    t.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            self._store_remaining(queue)

    def _deadline(self, queue: asyncio.Queue) -> float:
        """The shared drain deadline (monotonic), fixed by the first worker that sees the stop."""
        if self._drain_deadline is None:
            self._drain_deadline = time.monotonic() + max(0.0, float(self.drain_seconds))
            if queue.qsize():
                logger.info(
                    "pipeline stopping: processing %d queued item(s) for up to %.0fs",
                    queue.qsize(),
                    self.drain_seconds,
                )
        return self._drain_deadline

    async def _worker(self, queue: asyncio.Queue, stop: asyncio.Event) -> None:
        while not stop.is_set():
            try:
                item = await asyncio.wait_for(queue.get(), timeout=self.idle_poll_seconds)
            except TimeoutError:
                continue
            await self._run_item(queue, item)
        # graceful stop: finish what is already queued, within the drain deadline
        deadline = self._deadline(queue)
        while time.monotonic() < deadline:
            try:
                item = queue.get_nowait()
            except asyncio.QueueEmpty:
                return
            await self._run_item(queue, item)

    async def _run_item(self, queue: asyncio.Queue, item: IngestItem) -> None:
        try:
            await self.process(item)
        finally:
            queue.task_done()

    def _store_remaining(self, queue: asyncio.Queue) -> None:
        """Store (dedupe + insert + fast rules, no scoring, no alert) every item still queued. Never raises."""
        stored = dropped = 0
        while True:
            try:
                item = queue.get_nowait()
            except asyncio.QueueEmpty:
                break
            try:
                if self._store_only(item):
                    stored += 1
            except Exception:  # the store itself may be what failed; keep emptying the queue
                dropped += 1
                logger.exception("shutdown: could not store a queued %s item", getattr(item, "source", "?"))
            finally:
                queue.task_done()
        if stored or dropped:
            detail = f"{stored} queued post(s) stored without scoring or alerting at shutdown"
            if dropped:
                detail += f"; {dropped} could not be stored"
            logger.warning("pipeline stopped: %s", detail)
            try:
                self.store.log_event("shutdown_unprocessed", detail, level="warning")
            except Exception:
                logger.exception("could not record shutdown_unprocessed event")
        else:
            logger.info("pipeline stopped")

    def _store_only(self, item: IngestItem) -> bool:
        """Parse, dedupe and store one item (plus its fast-rule result). True if it was a new post."""
        post = payload_to_post(item.payload, item.received_at, item.source, self.watchlist)
        if post is None or not self.store.insert_post_if_new(post):
            return False
        account = self.find_account(post)
        if account is not None and post.source != "replay":
            self.store.set_since_id(post.author_id, post.id)
        try:  # nice to have (flagged / reasons in the DB); the post itself is already stored
            self.store.update_post_score(post.id, self.fast_rules.evaluate(post, account), None)
        except Exception:
            logger.debug("shutdown: fast rules failed for stored post %s", post.id, exc_info=True)
        return True


__all__ = [
    "EDIT_REALERT_MIN_RISE",
    "LATENCY_TARGET_SECONDS",
    "HandleResult",
    "Pipeline",
    "Scorer",
    "has_content",
    "is_repost",
    "payload_to_post",
]
