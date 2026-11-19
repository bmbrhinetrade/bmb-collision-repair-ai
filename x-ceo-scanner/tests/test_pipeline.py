"""payload_to_post parsing and Pipeline.handle: rules -> score -> decide -> alert, latency, storage.

Also: the scoring gate (every watched-account post is scored), rules-only cap, late (catch-up) posts,
edit re-alerts, and draining the queue on shutdown.
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from ceoscan.alerts.dispatcher import AlertDispatcher
from ceoscan.alerts.notifiers import CaptureNotifier
from ceoscan.config import AppConfig, Secrets, Settings
from ceoscan.models import (
    Account,
    IngestItem,
    IngestStatus,
    Post,
    ReferencedPost,
    RuleResult,
    Score,
    Watchlist,
)
from ceoscan.pipeline import (
    LATENCY_TARGET_SECONDS,
    Pipeline,
    has_content,
    payload_to_post,
)
from ceoscan.scoring.claude import OfflineScorer
from ceoscan.store import Store
from ceoscan.timeutil import datetime_to_snowflake, snowflake_to_datetime

NOON = datetime(2026, 11, 19, 20, 0, tzinfo=UTC)  # 12:00 PST - outside the 22:00-06:30 quiet hours
NIGHT = datetime(2026, 11, 20, 7, 30, tzinfo=UTC)  # 23:30 PST - inside quiet hours

ELON = Account(handle="elonmusk", tickers=("TSLA", "SPCX"), user_id="44196397")
VIP = Account(handle="saylor", tickers=("MSTR",), user_id="244647486", always_alert=True)
LISA = Account(handle="LisaSu", tickers=("AMD",))  # no user_id yet (not verified)
WATCHLIST = Watchlist(
    accounts=(ELON, VIP, LISA),
    keyword_rules=(),
    my_tickers=("TSLA", "SPCX", "MSTR", "AMD"),
    company_names={"TSLA": ("Tesla", "Cybercab"), "MSTR": ("Strategy",)},
)

HIGH_TEXT = "Tesla Q3: record 512,000 deliveries and we are raising full-year guidance."  # scripted 9, flagged
SEVEN_TEXT = "Tesla Q3 deliveries hit a record 512,000 vehicles."  # scripted 7, flagged
CHITCHAT = "Good morning everyone! Coffee time."  # not flagged, scripted 1
KEYWORD_TEXT = "Reports: US to exempt chips from new tariffs. $AMD up 4% premarket."  # scripted 8


# --------------------------------------------------------------------------- helpers


def sid(clock: datetime = NOON, seconds_before: float = 5.0, n: int = 0) -> str:
    """A snowflake id created `seconds_before` the clock (n disambiguates ids in the same ms)."""
    return str(datetime_to_snowflake(clock - timedelta(seconds=seconds_before)) + n)


def user(uid: str, username: str, name: str = "Someone", followers: int = 1000) -> dict[str, Any]:
    return {"id": uid, "username": username, "name": name, "verified": True,
            "public_metrics": {"followers_count": followers}}


def payload(
    post_id: str,
    text: str,
    author_id: str = "44196397",
    username: str | None = "elonmusk",
    *,
    extra: dict[str, Any] | None = None,
    users: list[dict[str, Any]] | None = None,
    tweets: list[dict[str, Any]] | None = None,
    tags: tuple[str, ...] = ("ceoscan:acct:r1t0:1",),
) -> dict[str, Any]:
    data: dict[str, Any] = {"id": post_id, "text": text, "author_id": author_id,
                            "edit_history_tweet_ids": [post_id]}
    data.update(extra or {})
    includes: dict[str, Any] = {}
    if users is not None:
        includes["users"] = users
    elif username is not None:
        includes["users"] = [user(author_id, username)]
    if tweets:
        includes["tweets"] = tweets
    return {"data": data, "includes": includes, "matching_rules": [{"id": str(i), "tag": t} for i, t in enumerate(tags)]}


def make_cfg(settings: Settings | None = None, watchlist: Watchlist = WATCHLIST) -> AppConfig:
    return AppConfig(settings or Settings(), watchlist, Secrets(), Path("settings.yaml"), Path("watchlist.yaml"))


class RecordingScorer:
    """OfflineScorer that records calls; optional fixed impact/direction per post."""

    def __init__(self, impact: int | None = None, direction: str = "unclear", raises: bool = False) -> None:
        self.inner = OfflineScorer()
        self.calls: list[tuple[Post, Account | None, RuleResult]] = []
        self.impact = impact
        self.direction = direction
        self.raises = raises

    async def score(self, post: Post, account: Account | None, rules: RuleResult,
                    my_tickers: tuple[str, ...], tz: str) -> Score:
        self.calls.append((post, account, rules))
        if self.raises:
            raise RuntimeError("scorer exploded")
        s = await self.inner.score(post, account, rules, my_tickers, tz)
        if self.impact is not None:
            s.impact = self.impact
            s.direction = self.direction  # type: ignore[assignment]
            s.source = "claude"
        return s


class ScriptedScorer:
    """Claude stand-in (source "claude"): impact/direction by post text, so these tests do not depend on
    the fast-rule heuristics. Tickers come from the rules, else the account (like a fallback would)."""

    def __init__(self, impacts: dict[str, int] | None = None, directions: dict[str, str] | None = None,
                 default: int = 1, source: str = "claude", tickers: dict[str, list[str]] | None = None) -> None:
        self.impacts = {HIGH_TEXT: 9, SEVEN_TEXT: 7, KEYWORD_TEXT: 8, **(impacts or {})}
        self.directions = directions or {}
        self.tickers = tickers or {}
        self.default = default
        self.source = source
        self.calls: list[tuple[Post, Account | None, RuleResult]] = []

    async def score(self, post: Post, account: Account | None, rules: RuleResult,
                    my_tickers: tuple[str, ...], tz: str) -> Score:
        self.calls.append((post, account, rules))
        tickers = self.tickers.get(post.text) or list(rules.tickers) or (list(account.tickers) if account else [])
        direction: Any = self.directions.get(post.text, "unclear")
        source: Any = self.source
        return Score(tickers=list(tickers), direction=direction, impact=self.impacts.get(post.text, self.default),
                     reason="scripted", is_joke=False, source=source)


class NoFlagRules:
    """Fast rules that never flag anything (to test the scoring gate independently of the regexes)."""

    def evaluate(self, post: Post, account: Account | None) -> RuleResult:
        return RuleResult(flagged=False)


class Env:
    def __init__(self, *, clock: datetime = NOON, settings: Settings | None = None,
                 scorer: Any = None, ok: bool = True, store: Store | None = None,
                 sms: CaptureNotifier | None = None, fast_rules: Any = None,
                 watchlist: Watchlist = WATCHLIST) -> None:
        self.settings = settings or Settings()
        self.cfg = make_cfg(self.settings, watchlist)
        self.store = store or Store(":memory:")
        self.capture = CaptureNotifier(ok=ok)
        self.sms = sms
        self.dispatcher = AlertDispatcher(self.settings.alerts, push=[self.capture], sms=sms)
        self.status = IngestStatus(mode="stream")
        self.scorer = scorer if scorer is not None else ScriptedScorer()
        self.clock = clock
        self.pipeline = Pipeline(self.cfg, self.store, self.scorer, self.dispatcher, self.status,
                                 fast_rules=fast_rules, clock=lambda: self.clock)

    def item(self, p: dict[str, Any], received_before: float = 2.0, source: str = "stream") -> IngestItem:
        return IngestItem(payload=p, received_at=self.clock - timedelta(seconds=received_before),
                          source=source)  # type: ignore[arg-type]

    def alerts(self) -> list[dict[str, Any]]:
        return [dict(r) for r in self.store._db.execute("SELECT * FROM alerts ORDER BY id").fetchall()]

    def events(self, kind: str) -> list[dict[str, Any]]:
        return [dict(r) for r in self.store._db.execute("SELECT * FROM events WHERE kind=?", (kind,)).fetchall()]


@pytest.fixture
def env() -> Iterator[Env]:
    e = Env()
    yield e
    e.store.close()


# --------------------------------------------------------------------------- payload_to_post


def test_payload_to_post_basic_fields() -> None:
    pid = sid()
    p = payload(pid, "Hello $tsla and $TSLA", extra={
        "lang": "en",
        "conversation_id": pid,
        "entities": {
            "cashtags": [{"start": 6, "end": 11, "tag": "tsla"}, {"start": 16, "end": 21, "tag": "TSLA"}],
            "mentions": [{"username": "sama"}],
            "urls": [{"url": "https://t.co/x", "expanded_url": "https://example.com/a"}, {"url": "https://t.co/y"}],
        },
    }, users=[user("44196397", "elonmusk", "Elon Musk", 220_000_000)], tags=("ceoscan:acct:r1t0:1", "ceoscan:kw:x:1"))
    received = NOON - timedelta(seconds=1)
    post = payload_to_post(p, received, "stream", WATCHLIST)

    assert post is not None
    assert post.id == pid and post.author_id == "44196397"
    assert post.text == "Hello $tsla and $TSLA"
    assert post.created_at == snowflake_to_datetime(pid)
    assert post.received_at == received and post.source == "stream"
    assert post.author_username == "elonmusk" and post.author_name == "Elon Musk"
    assert post.author_followers == 220_000_000 and post.author_verified is True
    assert post.cashtags == ["TSLA"]
    assert post.mentions == ["sama"]
    assert post.urls == ["https://example.com/a", "https://t.co/y"]
    assert post.matching_rule_tags == ["ceoscan:acct:r1t0:1", "ceoscan:kw:x:1"]
    assert post.edit_history_ids == [pid]
    assert post.conversation_id == pid and post.lang == "en"
    assert post.referenced == []
    assert post.raw is p
    assert post.url == f"https://x.com/elonmusk/status/{pid}"


def test_payload_to_post_prefers_note_tweet_long_text() -> None:
    full = "Robinhood Q3 results. " + "Lots of detail. " * 30 + "Also a $1B buyback for $HOOD."
    p = payload(sid(), full[:270] + "…", extra={
        "entities": {"cashtags": []},
        "note_tweet": {"text": full, "entities": {"cashtags": [{"start": 0, "end": 5, "tag": "HOOD"}]}},
    })
    post = payload_to_post(p, NOON, "stream", WATCHLIST)
    assert post is not None
    assert post.text == full
    assert "buyback" in post.text
    assert post.cashtags == ["HOOD"]  # from note_tweet.entities


def test_payload_to_post_referenced_quoted_and_replied_posts() -> None:
    quoted_id, parent_id, missing_id = sid(n=1), sid(n=2), sid(n=3)
    p = payload(
        sid(),
        "Yes. https://t.co/q",
        extra={"referenced_tweets": [
            {"type": "quoted", "id": quoted_id},
            {"type": "replied_to", "id": parent_id},
            {"type": "retweeted", "id": missing_id},
            {"type": "bogus", "id": "1"},
        ], "in_reply_to_user_id": "777"},
        users=[user("44196397", "elonmusk"), user("777", "some_fan")],
        tweets=[
            {"id": quoted_id, "text": "short", "note_tweet": {"text": "Strategy bought more BTC (full note)"},
             "author_id": "244647486"},  # VIP: username not in includes -> filled from watchlist
            {"id": parent_id, "text": "Will Tesla raise guidance?", "author_id": "777"},
        ],
    )
    post = payload_to_post(p, NOON, "stream", WATCHLIST)
    assert post is not None
    refs = {r.type: r for r in post.referenced}
    assert set(refs) == {"quoted", "replied_to", "retweeted"}
    assert refs["quoted"].text == "Strategy bought more BTC (full note)"
    assert refs["quoted"].author_id == "244647486" and refs["quoted"].author_username == "saylor"
    assert refs["replied_to"].text == "Will Tesla raise guidance?"
    assert refs["replied_to"].author_username == "some_fan"
    assert refs["retweeted"].text is None and refs["retweeted"].id == missing_id
    assert post.is_reply and post.is_quote
    assert post.in_reply_to_user_id == "777"


def test_payload_to_post_fills_username_from_watchlist() -> None:
    p = payload(sid(), "gm", author_id="244647486", username=None)
    post = payload_to_post(p, NOON, "poll", WATCHLIST)
    assert post is not None
    assert post.author_username == "saylor"
    assert post.author_name is None and post.author_followers is None


def test_payload_to_post_unknown_author_without_username() -> None:
    post = payload_to_post(payload(sid(), "gm", author_id="123", username=None), NOON, "stream", WATCHLIST)
    assert post is not None
    assert post.author_username is None
    assert post.url.endswith(f"/i/web/status/{post.id}")


def test_payload_to_post_accepts_int_ids_and_naive_received_at() -> None:
    pid = int(sid())
    p = {"data": {"id": pid, "text": "x", "author_id": 44196397}}
    naive = datetime(2026, 11, 19, 12, 0)  # noqa: DTZ001 - naive on purpose
    post = payload_to_post(p, naive, "stream", WATCHLIST)
    assert post is not None
    assert post.id == str(pid) and post.author_id == "44196397"
    assert post.received_at.tzinfo is not None
    assert post.author_username == "elonmusk"


@pytest.mark.parametrize(
    "bad",
    [
        None,
        "not a dict",
        [],
        {},
        {"errors": [{"title": "operational-disconnect"}]},
        {"data": "x"},
        {"data": {"text": "no id", "author_id": "1"}},
        {"data": {"id": "abc", "text": "x", "author_id": "1"}},
        {"data": {"id": "-5", "text": "x", "author_id": "1"}},
        {"data": {"id": True, "text": "x", "author_id": "1"}},
        {"data": {"id": "123", "text": "no author"}},
        {"data": {"id": "123", "author_id": "1"}},
        {"data": {"id": "123", "author_id": "1", "text": 42}},
        {"data": {"id": "1²3", "text": "x", "author_id": "1"}},
    ],
)
def test_payload_to_post_malformed_returns_none(bad: Any) -> None:
    assert payload_to_post(bad, NOON, "stream", WATCHLIST) is None


def test_payload_to_post_tolerates_junk_optional_fields() -> None:
    p = payload(sid(), "ok", extra={
        "entities": "junk", "referenced_tweets": [None, 5, {"type": "quoted"}],
        "edit_history_tweet_ids": ["x", None], "note_tweet": "junk",
    })
    p["includes"] = {"users": "junk", "tweets": [None]}
    p["matching_rules"] = [None, {"tag": None}, {"tag": "t"}]
    post = payload_to_post(p, NOON, "stream", WATCHLIST)
    assert post is not None
    assert post.text == "ok"
    assert post.referenced == [] and post.cashtags == [] and post.edit_history_ids == []
    assert post.matching_rule_tags == ["t"]
    assert post.author_username == "elonmusk"  # from the watchlist


# --------------------------------------------------------------------------- Pipeline.handle


async def test_high_scoring_post_alerts(env: Env) -> None:
    pid = sid()
    rec = await env.pipeline.handle(env.item(payload(pid, HIGH_TEXT)))

    assert rec is not None
    assert rec.post_id == pid and rec.kind == "post"
    assert rec.score == 9 and rec.tickers == ["TSLA"]
    assert rec.decision_reason == "score 9 >= 7"
    assert rec.sent_ok and not rec.quiet_hours
    assert len(env.capture.sent) == 1
    msg = env.capture.sent[0]
    assert "TSLA" in msg.title and "@elonmusk" in msg.title
    assert msg.priority == "urgent" and msg.url == f"https://x.com/elonmusk/status/{pid}"
    assert env.status.last_post_id == pid
    assert env.status.last_post_at == NOON - timedelta(seconds=2)


async def test_chitchat_without_watchlist_scoring_is_not_scored() -> None:
    settings = Settings()
    settings.scoring.score_watchlist_unflagged = False
    scorer = RecordingScorer()
    e = Env(scorer=scorer, settings=settings)
    pid = sid()
    res = await e.pipeline.process(e.item(payload(pid, CHITCHAT)))

    assert res.outcome == "no_alert" and res.alert is None
    assert res.decision is not None and res.decision.reason == "not flagged"
    assert scorer.calls == []
    assert e.capture.sent == []
    row = e.store.get_post_row(pid)
    assert row is not None  # every post is stored
    assert row["flagged"] == 0 and row["score"] is None and row["score_source"] == "skipped"
    assert e.alerts() == []
    e.store.close()


async def test_watched_account_chitchat_is_scored_but_does_not_alert() -> None:
    scorer = RecordingScorer()
    e = Env(scorer=scorer)  # score_watchlist_unflagged defaults to true
    pid = sid()
    res = await e.pipeline.process(e.item(payload(pid, CHITCHAT)))

    assert len(scorer.calls) == 1 and scorer.calls[0][1] == ELON
    assert not scorer.calls[0][2].flagged  # the fast rules are passed along as hints
    assert res.outcome == "no_alert" and res.decision is not None
    assert res.decision.reason.startswith("below threshold")
    row = e.store.get_post_row(pid)
    assert row["flagged"] == 0 and row["score"] is not None and row["score_source"] == "offline"
    assert e.capture.sent == []
    e.store.close()


async def test_flagged_below_threshold_is_stored_without_alert() -> None:
    e = Env(scorer=OfflineScorer())
    pid = sid()
    res = await e.pipeline.process(e.item(payload(pid, "Tesla is cool")))
    assert res.outcome == "no_alert"
    assert res.decision is not None and res.decision.reason.startswith("below threshold")
    row = e.store.get_post_row(pid)
    assert row["flagged"] == 1 and row["score"] == res.score.impact and row["score_source"] == "offline"
    e.store.close()


async def test_always_alert_account_alerts_on_chitchat() -> None:
    scorer = RecordingScorer()
    e = Env(scorer=scorer)
    rec = await e.pipeline.handle(e.item(payload(sid(), CHITCHAT, author_id="244647486", username="saylor")))

    assert rec is not None
    assert "always-alert account @saylor" in rec.decision_reason
    assert len(scorer.calls) == 1  # score_always_alert_unflagged: scored even though rules missed
    assert scorer.calls[0][1] == VIP
    assert e.capture.sent[0].priority == "normal"
    assert "Always-alert account" in e.capture.sent[0].body
    e.store.close()


async def test_always_alert_without_claude_scoring_still_alerts() -> None:
    settings = Settings()
    settings.scoring.score_always_alert_unflagged = False
    scorer = RecordingScorer()
    e = Env(settings=settings, scorer=scorer)
    pid = sid()
    rec = await e.pipeline.handle(e.item(payload(pid, CHITCHAT, author_id="244647486", username="saylor")))

    assert rec is not None and "always-alert" in rec.decision_reason
    assert scorer.calls == []  # no Claude call for an unflagged post
    assert e.store.get_post_row(pid)["score_source"] == "skipped"
    e.store.close()


async def test_quiet_hours_blocks_score_below_quiet_threshold() -> None:
    e = Env(clock=NIGHT)
    res = await e.pipeline.process(e.item(payload(sid(NIGHT), SEVEN_TEXT)))

    assert res.outcome == "no_alert"
    assert res.decision is not None and res.decision.quiet_hours
    assert res.decision.reason == "quiet hours: 7 < 9"
    assert e.capture.sent == []
    e.store.close()


async def test_quiet_hours_still_alerts_at_quiet_threshold() -> None:
    e = Env(clock=NIGHT)
    rec = await e.pipeline.handle(e.item(payload(sid(NIGHT), HIGH_TEXT)))

    assert rec is not None
    assert rec.quiet_hours is True
    assert rec.decision_reason == "quiet hours: score 9 >= 9"
    assert "quiet-hours" in e.capture.sent[0].tags
    assert e.alerts()[0]["quiet_hours"] == 1
    e.store.close()


async def test_same_post_outside_quiet_hours_alerts() -> None:
    e = Env(clock=NOON)
    rec = await e.pipeline.handle(e.item(payload(sid(NOON), SEVEN_TEXT)))
    assert rec is not None and rec.decision_reason == "score 7 >= 7" and not rec.quiet_hours
    e.store.close()


async def test_latency_fields_with_fixed_clock(env: Env) -> None:
    # created 5 s before the clock, received 2 s before it -> x=3 s, scoring=2 s, total=5 s + push
    rec = await env.pipeline.handle(env.item(payload(sid(seconds_before=5.0), HIGH_TEXT), received_before=2.0))

    assert rec is not None
    assert rec.x_delay_ms == 3000
    assert rec.scoring_ms == 2000
    assert rec.push_ms is not None and 0 <= rec.push_ms < 500
    assert rec.total_latency_ms == 5000 + rec.push_ms


async def test_alert_row_stored(env: Env) -> None:
    pid = sid()
    rec = await env.pipeline.handle(env.item(payload(pid, HIGH_TEXT)))
    assert rec is not None

    rows = env.alerts()
    assert len(rows) == 1
    row = rows[0]
    assert row["post_id"] == pid and row["kind"] == "post"
    assert row["score"] == 9 and row["direction"] == "unclear"
    assert json.loads(row["tickers"]) == ["TSLA"]
    assert row["decision_reason"] == "score 9 >= 7"
    assert row["sent_ok"] == 1
    assert row["title"] == rec.message.title and row["body"] == rec.message.body
    assert [c["channel"] for c in json.loads(row["channels"])] == ["capture"]
    assert (row["total_latency_ms"], row["x_delay_ms"], row["scoring_ms"], row["push_ms"]) == (
        rec.total_latency_ms, rec.x_delay_ms, rec.scoring_ms, rec.push_ms)
    assert env.store.alert_exists_for_post(pid)

    assert row["late"] == 0
    post_row = env.store.get_post_row(pid)
    assert post_row["score"] == 9 and post_row["flagged"] == 1 and post_row["score_source"] == "claude"
    assert json.loads(post_row["matching_rules"]) == ["ceoscan:acct:r1t0:1"]


async def test_alert_logs_latency_line(env: Env, caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.INFO, logger="ceoscan.pipeline")
    await env.pipeline.handle(env.item(payload(sid(), HIGH_TEXT)))
    lines = [r for r in caplog.records if r.getMessage().startswith("ALERT ")]
    assert len(lines) == 1 and lines[0].levelno == logging.INFO
    msg = lines[0].getMessage()
    assert msg.startswith("ALERT @elonmusk TSLA 9/10 unclear latency total=5.")
    assert "x=3.00s scoring=2.00s push=" in msg
    assert not [r for r in caplog.records if r.levelno == logging.WARNING]


async def test_slow_alert_logs_warning(env: Env, caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.INFO, logger="ceoscan.pipeline")
    rec = await env.pipeline.handle(env.item(payload(sid(seconds_before=20.0), HIGH_TEXT)))
    assert rec is not None and rec.total_latency_ms is not None
    assert rec.total_latency_ms > LATENCY_TARGET_SECONDS * 1000
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1 and "exceeds the 10 s target" in warnings[0].getMessage()


async def test_failed_delivery_recorded_without_total_latency() -> None:
    e = Env(ok=False)
    pid = sid()
    rec = await e.pipeline.handle(e.item(payload(pid, HIGH_TEXT)))

    assert rec is not None and not rec.sent_ok
    assert rec.total_latency_ms is None and rec.push_ms is None
    assert rec.x_delay_ms == 3000
    assert e.alerts()[0]["sent_ok"] == 0
    assert not e.store.alert_exists_for_post(pid)
    events = e.events("alert_failed")
    assert len(events) == 1 and events[0]["level"] == "error" and "simulated failure" in events[0]["detail"]
    e.store.close()


async def test_quoted_and_parent_context_reaches_scorer() -> None:
    """'On track. No change.' is only flagged because of the parent post; the scorer sees both."""
    scorer = RecordingScorer(impact=8, direction="bullish")
    e = Env(scorer=scorer)
    parent = sid(n=7)
    p = payload(sid(), "On track. No change.", extra={"referenced_tweets": [{"type": "replied_to", "id": parent}]},
                users=[user("44196397", "elonmusk"), user("555", "fan")],
                tweets=[{"id": parent, "text": "Is Tesla still on track for Q2 Cybercab production, or will "
                                                "guidance slip?", "author_id": "555"}])
    rec = await e.pipeline.handle(e.item(p))

    assert len(scorer.calls) == 1
    post, account, rules = scorer.calls[0]
    assert account == ELON
    assert rules.flagged and "TSLA" in rules.tickers
    assert post.referenced[0].type == "replied_to" and "Cybercab" in (post.referenced[0].text or "")
    assert rec is not None and rec.direction == "bullish" and rec.score == 8
    assert "Reply to @fan" in e.capture.sent[0].body
    e.store.close()


async def test_keyword_rule_post_from_unwatched_account() -> None:
    e = Env()
    p = payload(sid(), KEYWORD_TEXT,
                author_id="999", username="bigmacro", tags=("ceoscan:kw:big-account-cashtags:1",),
                extra={"entities": {"cashtags": [{"tag": "AMD"}]}})
    res = await e.pipeline.process(e.item(p))
    assert res.account is None
    assert res.outcome == "alert" and res.alert is not None and "AMD" in res.alert.tickers
    assert e.store.get_since_id("999") is None  # since_ids only for watched accounts
    e.store.close()


async def test_since_id_updated_for_watched_accounts_only(env: Env) -> None:
    first, second = sid(n=1), sid(n=2)
    await env.pipeline.handle(env.item(payload(second, CHITCHAT)))
    await env.pipeline.handle(env.item(payload(first, CHITCHAT)))  # older id arrives later
    assert env.store.get_since_id("44196397") == second
    await env.pipeline.handle(env.item(payload(sid(n=3), CHITCHAT, author_id="12345", username="stranger")))
    assert env.store.get_since_id("12345") is None


async def test_replay_source_does_not_move_since_id(env: Env) -> None:
    await env.pipeline.handle(env.item(payload(sid(), HIGH_TEXT), source="replay"))
    assert env.store.get_since_id("44196397") is None


async def test_account_matched_by_username_when_no_user_id(env: Env) -> None:
    res = await env.pipeline.process(env.item(payload(sid(), CHITCHAT, author_id="31337", username="lisasu")))
    assert res.account == LISA  # case-insensitive handle match


async def test_username_match_refused_when_watchlist_pins_other_id(env: Env) -> None:
    res = await env.pipeline.process(env.item(payload(sid(), CHITCHAT, author_id="666", username="saylor")))
    assert res.account is None  # handle now used by someone else: not our always-alert account
    assert res.alert is None


async def test_edit_of_alerted_post_does_not_alert_again(env: Env) -> None:
    edited = HIGH_TEXT.replace("record", "a record")
    env.pipeline.scorer = ScriptedScorer({edited: 9})
    original = sid(n=1)
    assert await env.pipeline.handle(env.item(payload(original, HIGH_TEXT))) is not None
    edit = sid(n=2)
    res = await env.pipeline.process(env.item(payload(
        edit, edited, extra={"edit_history_tweet_ids": [original, edit]})))

    assert res.outcome == "edit_skipped" and res.alert is None
    assert original in res.detail and "no material change" in res.detail
    assert len(env.capture.sent) == 1
    assert env.store.get_post_row(edit) is not None  # still stored


async def test_edit_with_higher_score_alerts_again(env: Env) -> None:
    original = sid(n=1)
    assert await env.pipeline.handle(env.item(payload(original, SEVEN_TEXT))) is not None  # score 7
    edit = sid(n=2)
    rec = await env.pipeline.handle(env.item(payload(
        edit, HIGH_TEXT, extra={"edit_history_tweet_ids": [original, edit]})))  # score 9
    assert rec is not None and rec.score == 9
    assert len(env.capture.sent) == 2
    msg = env.capture.sent[1]
    assert "✏️ Edited: score was 7/10, now 9/10" in msg.body
    assert msg.title.endswith("(edited)") and "edited" in msg.tags


async def test_malformed_payload_is_logged_and_dropped(env: Env) -> None:
    res = await env.pipeline.process(env.item({"data": {"text": "no id"}}))
    assert res.outcome == "malformed" and res.post is None
    assert await env.pipeline.handle(env.item({"errors": [{"title": "x"}]})) is None
    assert len(env.events("malformed_payload")) == 2
    assert env.capture.sent == []


async def test_exception_is_recorded_as_pipeline_error() -> None:
    e = Env(scorer=RecordingScorer(raises=True))
    pid = sid()
    res = await e.pipeline.process(e.item(payload(pid, HIGH_TEXT)))

    assert res.outcome == "error" and res.alert is None
    assert "RuntimeError" in res.detail
    events = e.events("pipeline_error")
    assert len(events) == 1 and events[0]["level"] == "error" and pid in events[0]["detail"]
    assert e.capture.sent == []
    # the pipeline keeps going afterwards
    e.pipeline.scorer = ScriptedScorer()
    assert await e.pipeline.handle(e.item(payload(sid(n=1), HIGH_TEXT))) is not None
    e.store.close()


async def test_require_my_ticker_blocks_unrelated_tickers() -> None:
    settings = Settings()
    e = Env(settings=settings, scorer=RecordingScorer(impact=9))
    p = payload(sid(), "$GME earnings guidance raised, 50% beat", author_id="999", username="stranger")
    res = await e.pipeline.process(e.item(p))
    assert res.outcome == "no_alert"
    assert res.decision is not None and res.decision.reason.startswith("no watched ticker")
    e.store.close()


async def test_run_processes_queue_until_stopped(env: Env) -> None:
    env.pipeline.idle_poll_seconds = 0.01
    queue: asyncio.Queue[IngestItem] = asyncio.Queue()
    for n, text in enumerate([HIGH_TEXT, CHITCHAT, SEVEN_TEXT]):
        queue.put_nowait(env.item(payload(sid(n=n), text)))
    stop = asyncio.Event()
    runner = asyncio.create_task(env.pipeline.run(queue, stop, workers=2))
    await asyncio.wait_for(queue.join(), timeout=5)
    stop.set()
    await asyncio.wait_for(runner, timeout=5)

    assert len(env.capture.sent) == 2
    assert int(env.store._db.execute("SELECT COUNT(*) FROM posts").fetchone()[0]) == 3


async def test_run_stops_promptly_when_idle(env: Env) -> None:
    env.pipeline.idle_poll_seconds = 0.01
    stop = asyncio.Event()
    runner = asyncio.create_task(env.pipeline.run(asyncio.Queue(), stop, workers=3))
    await asyncio.sleep(0.05)
    stop.set()
    await asyncio.wait_for(runner, timeout=1)



# --------------------------------------------------------------------------- scoring gate


def _post(text: str, **kw: Any) -> Post:
    return Post(id="1", text=text, author_id="2", created_at=NOON, received_at=NOON, source="stream", **kw)


def test_has_content() -> None:
    assert has_content(_post("Funding secured."))  # two words, still worth a Claude call
    assert has_content(_post("10"))
    assert has_content(_post("決算発表"))  # any script's letters count
    assert not has_content(_post("🔥🚀 !!!"))
    assert not has_content(_post("   "))
    assert has_content(_post("🔥", urls=["https://example.com/a"]))
    assert has_content(_post("👀", referenced=[ReferencedPost(type="quoted", id="5")]))
    assert has_content(_post("📈", raw={"data": {"attachments": {"media_keys": ["3_1"]}}}))
    assert not has_content(_post("📈", raw={"data": {"attachments": "junk"}}))


async def test_unflagged_post_from_watched_account_is_scored_and_can_alert() -> None:
    """A CEO post no keyword rule catches ("step down", "funding secured") still reaches Claude."""
    text = "After 12 incredible years I have decided it is time for someone new to lead."
    scorer = ScriptedScorer({text: 10}, directions={text: "bearish"})
    e = Env(scorer=scorer, fast_rules=NoFlagRules())
    res = await e.pipeline.process(e.item(payload(sid(), text, author_id="31337", username="LisaSu")))

    assert res.rules is not None and not res.rules.flagged
    assert len(scorer.calls) == 1 and scorer.calls[0][1] == LISA
    assert res.outcome == "alert" and res.alert is not None
    assert res.alert.tickers == ["AMD"] and res.alert.direction == "bearish"
    e.store.close()


async def test_unflagged_post_from_unwatched_account_stays_gated() -> None:
    scorer = ScriptedScorer({KEYWORD_TEXT: 9})
    e = Env(scorer=scorer, fast_rules=NoFlagRules())
    res = await e.pipeline.process(e.item(payload(sid(), KEYWORD_TEXT, author_id="999", username="bigmacro",
                                                  tags=("ceoscan:kw:big-account-cashtags:1",))))
    assert scorer.calls == []
    assert res.outcome == "no_alert" and res.decision is not None and res.decision.reason == "not flagged"
    e.store.close()


async def test_content_free_watched_post_is_not_scored() -> None:
    scorer = ScriptedScorer()
    e = Env(scorer=scorer, fast_rules=NoFlagRules())
    res = await e.pipeline.process(e.item(payload(sid(n=1), "🔥🔥🔥")))
    assert scorer.calls == [] and res.decision is not None and res.decision.reason == "not flagged"

    # the same emoji with a link, a quoted post or media is scored
    await e.pipeline.process(e.item(payload(sid(n=2), "🔥", extra={
        "entities": {"urls": [{"url": "https://t.co/a", "expanded_url": "https://tesla.com/news"}]}})))
    await e.pipeline.process(e.item(payload(sid(n=3), "🔥", extra={
        "referenced_tweets": [{"type": "quoted", "id": sid(n=9)}]})))
    await e.pipeline.process(e.item(payload(sid(n=4), "🔥", extra={"attachments": {"media_keys": ["3_1"]}})))
    assert len(scorer.calls) == 3
    e.store.close()


async def test_unflagged_reply_or_repost_respects_account_settings() -> None:
    quiet_elon = Account(handle="elonmusk", tickers=("TSLA",), user_id="44196397",
                         include_replies=False, include_retweets=False)
    wl = Watchlist(accounts=(quiet_elon,), keyword_rules=(), my_tickers=("TSLA",), company_names={})
    scorer = ScriptedScorer()
    e = Env(scorer=scorer, fast_rules=NoFlagRules(), watchlist=wl)
    await e.pipeline.process(e.item(payload(sid(n=1), "Yes", extra={
        "referenced_tweets": [{"type": "replied_to", "id": sid(n=8)}]})))
    await e.pipeline.process(e.item(payload(sid(n=2), "RT @someone: big news", extra={
        "referenced_tweets": [{"type": "retweeted", "id": sid(n=9)}]})))
    assert scorer.calls == []
    await e.pipeline.process(e.item(payload(sid(n=3), "Big news tomorrow")))  # an original post is scored
    assert len(scorer.calls) == 1
    e.store.close()


async def test_flagged_reply_is_scored_even_when_replies_are_excluded() -> None:
    quiet_elon = Account(handle="elonmusk", tickers=("TSLA",), user_id="44196397", include_replies=False)
    wl = Watchlist(accounts=(quiet_elon,), keyword_rules=(), my_tickers=("TSLA",),
                   company_names={"TSLA": ("Tesla",)})
    scorer = ScriptedScorer()
    e = Env(scorer=scorer, watchlist=wl)
    res = await e.pipeline.process(e.item(payload(sid(), HIGH_TEXT, extra={
        "referenced_tweets": [{"type": "replied_to", "id": sid(n=8)}]})))
    assert res.rules is not None and res.rules.flagged
    assert len(scorer.calls) == 1 and res.outcome == "alert"
    e.store.close()


# --------------------------------------------------------------------------- rules-only cap + label


@pytest.mark.parametrize("source", ["rules_fallback", "offline"])
async def test_rules_only_scores_are_capped_and_labelled(source: str) -> None:
    sms = CaptureNotifier(name="sms")
    e = Env(scorer=ScriptedScorer({HIGH_TEXT: 10}, source=source), sms=sms)
    rec = await e.pipeline.handle(e.item(payload(sid(), HIGH_TEXT)))

    assert rec is not None and rec.score == 7  # scoring.fallback_max_impact
    msg = e.capture.sent[0]
    assert msg.priority == "high"  # never urgent on keywords alone
    assert msg.title.endswith("(rules-only)") and "rules-only" in msg.tags
    assert "Rules-only score" in msg.body
    assert (msg.sms_text or "").startswith("@elonmusk TSLA UNCLEAR 7/10 (rules-only):")
    assert sms.sent == []  # 7 < alerts.sms.min_score 8
    assert e.store.get_post_row(rec.post_id)["score"] == 7
    e.store.close()


async def test_rules_only_cap_is_configurable() -> None:
    settings = Settings()
    settings.scoring.fallback_max_impact = 5
    e = Env(settings=settings, scorer=ScriptedScorer({HIGH_TEXT: 10}, source="rules_fallback"))
    res = await e.pipeline.process(e.item(payload(sid(), HIGH_TEXT)))
    assert res.score is not None and res.score.impact == 5
    assert res.outcome == "no_alert"
    e.store.close()


async def test_skipped_always_alert_score_is_capped() -> None:
    settings = Settings()
    settings.scoring.score_always_alert_unflagged = False

    class TenRules:
        def evaluate(self, post: Post, account: Account | None) -> RuleResult:
            return RuleResult(flagged=False, rule_score=10, own_rule_score=10, tickers=["MSTR"])

    e = Env(settings=settings, fast_rules=TenRules())
    rec = await e.pipeline.handle(e.item(payload(sid(), CHITCHAT, author_id="244647486", username="saylor")))
    assert rec is not None and rec.score is not None and rec.score <= 7
    assert e.store.get_post_row(rec.post_id)["score_source"] == "skipped"
    assert "(rules-only)" in e.capture.sent[0].title
    e.store.close()


async def test_claude_scores_are_not_capped(env: Env) -> None:
    rec = await env.pipeline.handle(env.item(payload(sid(), HIGH_TEXT)))
    assert rec is not None and rec.score == 9
    assert "rules-only" not in env.capture.sent[0].title


# --------------------------------------------------------------------------- late posts (outage catch-up)

TWO_HOURS = 2 * 3600.0


async def test_late_post_alerts_low_key_without_sms() -> None:
    sms = CaptureNotifier(name="sms")
    e = Env(sms=sms)
    pid = sid(seconds_before=TWO_HOURS + 2)  # posted 2 h before we received it (gap recovery)
    res = await e.pipeline.process(e.item(payload(pid, HIGH_TEXT), source="recovery"))

    assert res.outcome == "alert" and res.alert is not None
    rec = res.alert
    assert rec.late is True and rec.score == 9
    assert res.decision is not None and res.decision.priority == "normal"  # never urgent
    assert "late: posted 2h ago" in rec.decision_reason
    msg = e.capture.sent[0]
    assert msg.priority == "normal"
    assert msg.title.startswith("Missed during outage - posted 2h ago: ")
    assert "late" in msg.tags
    assert (msg.sms_text or "").startswith("[Missed during outage - posted 2h ago] @elonmusk")
    assert any(line.startswith("⏪ Late:") for line in msg.body.split("\n"))
    assert sms.sent == []  # score 9 >= sms.min_score 8, but late alerts never go out by SMS
    assert [r.channel for r in rec.results] == ["capture"]

    row = e.alerts()[0]
    assert row["late"] == 1 and row["total_latency_ms"] == rec.total_latency_ms
    assert rec.total_latency_ms is not None and rec.total_latency_ms > TWO_HOURS * 1000
    # stored and scored like any other post
    assert e.store.get_post_row(pid)["score"] == 9
    e.store.close()


async def test_late_post_is_still_emailed_but_never_sent_by_sms() -> None:
    """The dispatcher gets the real score: email (score >= email.min_score) still goes out; SMS never."""
    sms, email = CaptureNotifier(name="sms"), CaptureNotifier(name="email")
    e = Env(sms=sms)
    e.pipeline.dispatcher = AlertDispatcher(e.settings.alerts, push=[e.capture], sms=sms, email=email)
    rec = await e.pipeline.handle(e.item(payload(sid(seconds_before=TWO_HOURS), HIGH_TEXT), source="recovery"))
    assert rec is not None and rec.late and rec.score == 9
    assert sms.sent == [] and len(email.sent) == 1 and "late" in email.sent[0].tags
    assert sorted(r.channel for r in rec.results) == ["capture", "email"]
    e.store.close()


async def test_catch_up_alerts_are_left_out_of_latency_stats(caplog: pytest.LogCaptureFixture) -> None:
    """A recovered post younger than max_post_age is not 'late', but measures the outage, not the live path."""
    caplog.set_level(logging.INFO, logger="ceoscan.pipeline")
    e = Env()
    await e.pipeline.handle(e.item(payload(sid(n=1), HIGH_TEXT)))  # live: ~5 s
    rec = await e.pipeline.handle(e.item(payload(sid(seconds_before=300, n=2), HIGH_TEXT), source="recovery"))
    assert rec is not None and not rec.late and rec.total_latency_ms is not None and rec.total_latency_ms >= 300_000
    stats = e.store.stats_since(NOON - timedelta(days=1))
    assert stats["alerts"] == 2 and stats["max_latency_ms"] is not None and stats["max_latency_ms"] < 10_000
    assert not [r for r in caplog.records if "exceeds the 10 s target" in r.getMessage()]
    assert any("catch-up; not counted in latency stats" in r.getMessage() for r in caplog.records)
    e.store.close()


@pytest.mark.parametrize(("impact", "priority"), [(7, "low"), (8, "low"), (9, "normal"), (10, "normal")])
async def test_late_post_priority(impact: int, priority: str) -> None:
    e = Env(scorer=ScriptedScorer({HIGH_TEXT: impact}))
    rec = await e.pipeline.handle(e.item(payload(sid(seconds_before=TWO_HOURS), HIGH_TEXT), source="poll"))
    assert rec is not None and rec.late and e.capture.sent[0].priority == priority
    e.store.close()


async def test_post_within_max_age_is_not_late() -> None:
    sms = CaptureNotifier(name="sms")
    e = Env(sms=sms)
    rec = await e.pipeline.handle(e.item(payload(sid(seconds_before=600), HIGH_TEXT), source="recovery"))
    assert rec is not None and not rec.late  # 10 min < alerts.max_post_age_seconds (15 min)
    assert e.capture.sent[0].priority == "urgent" and len(sms.sent) == 1
    e.store.close()


async def test_late_post_in_quiet_hours_still_needs_quiet_threshold() -> None:
    e = Env(clock=NIGHT)
    res = await e.pipeline.process(e.item(payload(sid(NIGHT, seconds_before=TWO_HOURS), SEVEN_TEXT)))
    assert res.outcome == "no_alert" and res.decision is not None and res.decision.reason == "quiet hours: 7 < 9"
    e.store.close()


async def test_late_alert_logged_without_latency_warning(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.INFO, logger="ceoscan.pipeline")
    e = Env()
    await e.pipeline.handle(e.item(payload(sid(seconds_before=TWO_HOURS), HIGH_TEXT), source="recovery"))
    assert any(r.getMessage().startswith("LATE ALERT @elonmusk TSLA 9/10") for r in caplog.records)
    assert not [r for r in caplog.records if "exceeds the 10 s target" in r.getMessage()]
    e.store.close()


async def test_late_alerts_are_left_out_of_latency_stats() -> None:
    e = Env()
    await e.pipeline.handle(e.item(payload(sid(n=1), HIGH_TEXT)))  # live: ~5 s
    await e.pipeline.handle(e.item(payload(sid(seconds_before=3 * 86400, n=2), SEVEN_TEXT), source="poll"))
    stats = e.store.stats_since(NOON - timedelta(days=7))
    assert stats["alerts"] == 2 and stats["alerts_late"] == 1
    assert stats["max_latency_ms"] is not None and stats["max_latency_ms"] < 10_000
    assert stats["avg_latency_ms"] == stats["max_latency_ms"]
    e.store.close()


# --------------------------------------------------------------------------- edits


def _edit_env(texts: dict[str, tuple[int, str]], tickers: dict[str, list[str]] | None = None,
              source: str = "claude") -> Env:
    scorer = ScriptedScorer({t: v[0] for t, v in texts.items()}, directions={t: v[1] for t, v in texts.items()},
                            tickers=tickers, source=source)
    return Env(scorer=scorer)


async def _post_then_edit(e: Env, first: str, second: str) -> tuple[Any, Any]:
    original, edit = sid(n=1), sid(n=2)
    a = await e.pipeline.process(e.item(payload(original, first)))
    b = await e.pipeline.process(e.item(payload(edit, second, extra={"edit_history_tweet_ids": [original, edit]})))
    return a, b


async def test_edit_that_flips_direction_alerts_again() -> None:
    up, down = "Tesla will raise guidance", "Tesla will NOT raise guidance"
    e = _edit_env({up: (9, "bullish"), down: (9, "bearish")})
    a, b = await _post_then_edit(e, up, down)

    assert a.outcome == "alert" and b.outcome == "alert"
    msg = e.capture.sent[1]
    assert "✏️ Edited: was BULLISH, now BEARISH" in msg.body
    assert msg.title.startswith("🔴 TSLA -9/10 @elonmusk") and msg.title.endswith("(edited)")
    assert "(edited)" in (msg.sms_text or "")
    e.store.close()


async def test_edit_with_small_score_rise_does_not_alert_again() -> None:
    v1, v2 = "Tesla deliveries up", "Tesla deliveries up a lot"
    e = _edit_env({v1: (7, "bullish"), v2: (8, "bullish")})
    _a, b = await _post_then_edit(e, v1, v2)
    assert b.outcome == "edit_skipped" and len(e.capture.sent) == 1
    e.store.close()


async def test_edit_to_or_from_unclear_does_not_alert_again() -> None:
    v1, v2 = "Tesla news", "Tesla news (typo fixed)"
    e = _edit_env({v1: (8, "bullish"), v2: (8, "unclear")})
    _a, b = await _post_then_edit(e, v1, v2)
    assert b.outcome == "edit_skipped"
    e.store.close()


async def test_edit_that_names_a_new_watched_ticker_alerts_again() -> None:
    v1, v2 = "Tesla and partner news", "Tesla and AMD partner news"
    e = _edit_env({v1: (8, "bullish"), v2: (8, "bullish")}, tickers={v1: ["TSLA"], v2: ["TSLA", "AMD", "XYZ"]})
    _a, b = await _post_then_edit(e, v1, v2)
    assert b.outcome == "alert"
    assert "✏️ Edited: now also names AMD" in e.capture.sent[1].body  # XYZ is not one of my tickers
    e.store.close()


async def test_rules_only_edit_never_realerts_over_a_claude_alert() -> None:
    v1, v2 = "Tesla will raise guidance", "Tesla will NOT raise guidance"
    e = _edit_env({v1: (7, "bullish"), v2: (7, "bearish")})
    original, edit = sid(n=1), sid(n=2)
    assert await e.pipeline.handle(e.item(payload(original, v1))) is not None
    e.pipeline.scorer = ScriptedScorer({v2: 10}, source="rules_fallback")  # Claude down for the edit
    res = await e.pipeline.process(e.item(payload(edit, v2, extra={"edit_history_tweet_ids": [original, edit]})))
    assert res.outcome == "edit_skipped" and len(e.capture.sent) == 1
    e.store.close()


async def test_edit_is_compared_with_the_latest_alerted_version() -> None:
    v1, v2, v3 = "Tesla will raise guidance", "Tesla will NOT raise guidance", "Tesla will NOT raise guidance."
    e = _edit_env({v1: (9, "bullish"), v2: (9, "bearish"), v3: (9, "bearish")})
    i1, i2, i3 = sid(n=1), sid(n=2), sid(n=3)
    assert (await e.pipeline.process(e.item(payload(i1, v1)))).outcome == "alert"
    r2 = await e.pipeline.process(e.item(payload(i2, v2, extra={"edit_history_tweet_ids": [i1, i2]})))
    r3 = await e.pipeline.process(e.item(payload(i3, v3, extra={"edit_history_tweet_ids": [i1, i2, i3]})))
    assert r2.outcome == "alert"
    assert r3.outcome == "edit_skipped" and i2 in r3.detail  # bearish -> bearish: nothing new since v2
    assert len(e.capture.sent) == 2
    e.store.close()


async def test_edit_of_post_whose_alert_failed_alerts() -> None:
    e = Env(ok=False)
    original, edit = sid(n=1), sid(n=2)
    await e.pipeline.handle(e.item(payload(original, HIGH_TEXT)))
    e.capture.ok = True
    e.pipeline.scorer = ScriptedScorer({SEVEN_TEXT: 9})
    rec = await e.pipeline.handle(e.item(payload(edit, SEVEN_TEXT, extra={"edit_history_tweet_ids": [original, edit]})))
    assert rec is not None and rec.sent_ok  # the user never saw the first version
    e.store.close()


# --------------------------------------------------------------------------- shutdown drain


async def test_stop_drains_already_queued_items(env: Env) -> None:
    """SIGTERM while recovery has just queued posts: they are still stored, scored and alerted."""
    env.pipeline.idle_poll_seconds = 0.01
    queue: asyncio.Queue[IngestItem] = asyncio.Queue()
    ids = [sid(n=n) for n in range(3)]
    for pid in ids:
        queue.put_nowait(env.item(payload(pid, HIGH_TEXT), source="poll"))
    stop = asyncio.Event()
    stop.set()  # stop arrived before the workers took anything
    await asyncio.wait_for(env.pipeline.run(queue, stop, workers=4), timeout=5)

    assert queue.empty()
    assert all(env.store.get_post_row(pid)["score"] == 9 for pid in ids)
    assert len(env.capture.sent) == 3
    assert env.store.get_since_id("44196397") == max(ids, key=int)


async def test_items_left_after_drain_deadline_are_stored_unscored(env: Env) -> None:
    env.pipeline.drain_seconds = 0.0
    queue: asyncio.Queue[IngestItem] = asyncio.Queue()
    ids = [sid(n=n) for n in range(3)]
    for pid in ids:
        queue.put_nowait(env.item(payload(pid, HIGH_TEXT), source="poll"))
    stop = asyncio.Event()
    stop.set()
    await asyncio.wait_for(env.pipeline.run(queue, stop, workers=2), timeout=5)
    await asyncio.wait_for(queue.join(), timeout=1)  # task_done for every item

    assert env.capture.sent == []
    for pid in ids:
        row = env.store.get_post_row(pid)
        assert row is not None and row["flagged"] == 1 and row["score"] is None
    assert env.store.get_since_id("44196397") == max(ids, key=int)
    events = env.events("shutdown_unprocessed")
    assert len(events) == 1 and events[0]["level"] == "warning" and events[0]["detail"].startswith("3 queued")


async def test_drain_deadline_bounds_shutdown_with_slow_scoring() -> None:
    class SlowScorer(ScriptedScorer):
        async def score(self, *args: Any, **kwargs: Any) -> Score:
            await asyncio.sleep(0.15)
            return await super().score(*args, **kwargs)

    e = Env(scorer=SlowScorer())
    e.pipeline.drain_seconds = 0.2
    queue: asyncio.Queue[IngestItem] = asyncio.Queue()
    ids = [sid(n=n) for n in range(10)]
    for pid in ids:
        queue.put_nowait(e.item(payload(pid, HIGH_TEXT)))
    stop = asyncio.Event()
    stop.set()
    loop = asyncio.get_running_loop()
    t0 = loop.time()
    await asyncio.wait_for(e.pipeline.run(queue, stop, workers=1), timeout=5)
    assert loop.time() - t0 < 1.0

    scored = [pid for pid in ids if e.store.get_post_row(pid)["score"] is not None]
    assert 1 <= len(scored) < len(ids)  # some fully processed within the deadline ...
    assert all(e.store.get_post_row(pid) is not None for pid in ids)  # ... and every post stored
    assert queue.empty()
    e.store.close()


async def test_cancelled_pipeline_still_stores_queued_items(env: Env) -> None:
    env.pipeline.idle_poll_seconds = 0.01
    queue: asyncio.Queue[IngestItem] = asyncio.Queue()
    stop = asyncio.Event()
    runner = asyncio.create_task(env.pipeline.run(queue, stop, workers=1))
    await asyncio.sleep(0.02)

    class Blocker(ScriptedScorer):
        async def score(self, *args: Any, **kwargs: Any) -> Score:
            await asyncio.sleep(10)
            raise AssertionError("unreachable")

    env.pipeline.scorer = Blocker()
    first, second = sid(n=1), sid(n=2)
    queue.put_nowait(env.item(payload(first, HIGH_TEXT)))
    queue.put_nowait(env.item(payload(second, HIGH_TEXT)))
    await asyncio.sleep(0.05)  # the worker is stuck scoring the first post
    runner.cancel()  # forced shutdown (second signal)
    with pytest.raises(asyncio.CancelledError):
        await runner
    assert env.store.get_post_row(first) is not None  # stored before scoring started
    assert env.store.get_post_row(second) is not None  # stored by the shutdown sweep
    assert queue.empty()
