"""payload_to_post parsing and Pipeline.handle: rules -> score -> decide -> alert, latency, storage."""

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
    RuleResult,
    Score,
    Watchlist,
)
from ceoscan.pipeline import LATENCY_TARGET_SECONDS, Pipeline, payload_to_post
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

HIGH_TEXT = "Tesla Q3: record 512,000 deliveries and we are raising full-year guidance."  # rules score 9
SEVEN_TEXT = "Tesla Q3 deliveries hit a record 512,000 vehicles."  # rules score 7
CHITCHAT = "Good morning everyone! Coffee time."  # not flagged


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


class Env:
    def __init__(self, *, clock: datetime = NOON, settings: Settings | None = None,
                 scorer: Any = None, ok: bool = True, store: Store | None = None) -> None:
        self.settings = settings or Settings()
        self.cfg = make_cfg(self.settings)
        self.store = store or Store(":memory:")
        self.capture = CaptureNotifier(ok=ok)
        self.dispatcher = AlertDispatcher(self.settings.alerts, push=[self.capture])
        self.status = IngestStatus(mode="stream")
        self.scorer = scorer if scorer is not None else OfflineScorer(self.settings.scoring)
        self.clock = clock
        self.pipeline = Pipeline(self.cfg, self.store, self.scorer, self.dispatcher, self.status,
                                 clock=lambda: self.clock)

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


async def test_chitchat_does_not_alert_and_is_not_scored() -> None:
    scorer = RecordingScorer()
    e = Env(scorer=scorer)
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


async def test_flagged_below_threshold_is_stored_without_alert(env: Env) -> None:
    pid = sid()
    res = await env.pipeline.process(env.item(payload(pid, "Tesla is cool")))
    assert res.outcome == "no_alert"
    assert res.decision is not None and res.decision.reason.startswith("below threshold")
    row = env.store.get_post_row(pid)
    assert row["flagged"] == 1 and row["score"] == res.score.impact and row["score_source"] == "offline"


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

    post_row = env.store.get_post_row(pid)
    assert post_row["score"] == 9 and post_row["flagged"] == 1 and post_row["score_source"] == "offline"
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
    p = payload(sid(), "Reports: US to exempt chips from new tariffs. $AMD up 4% premarket.",
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
    original = sid(n=1)
    assert await env.pipeline.handle(env.item(payload(original, HIGH_TEXT))) is not None
    edit = sid(n=2)
    res = await env.pipeline.process(env.item(payload(
        edit, HIGH_TEXT.replace("record", "a record"), extra={"edit_history_tweet_ids": [original, edit]})))

    assert res.outcome == "edit_skipped" and res.alert is None
    assert original in res.detail
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
    assert "Edited post" in env.capture.sent[1].body


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
    e.pipeline.scorer = OfflineScorer()
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

