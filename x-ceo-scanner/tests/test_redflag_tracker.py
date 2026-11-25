"""Red-flag tracker (spreading, denials, hijack cooldowns), its store table and migration, the red-flag path
through the pipeline (decide -> format -> dispatch -> record -> desk / Duet), and the /health + summary counters."""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from ceoscan.alerts.dispatcher import AlertDispatcher
from ceoscan.alerts.notifiers import CaptureNotifier
from ceoscan.config import AppConfig, RedFlagSettings, Secrets, Settings
from ceoscan.models import (
    Account,
    AlertMessage,
    AlertRecord,
    ChannelResult,
    Credibility,
    IngestItem,
    IngestStatus,
    Post,
    RedFlagSignal,
    ReferencedPost,
    RuleResult,
    Score,
    Watchlist,
)
from ceoscan.ops.health import HealthServer
from ceoscan.ops.scheduler import OpsScheduler
from ceoscan.pipeline import Pipeline
from ceoscan.redflag_tracker import KV_SPREAD_PREFIX, RedFlagTracker
from ceoscan.store import Store
from ceoscan.timeutil import datetime_to_snowflake

NOON = datetime(2026, 11, 19, 20, 0, tzinfo=UTC)  # 12:00 PT
NIGHT = datetime(2026, 11, 20, 7, 30, tzinfo=UTC)  # 23:30 PT (quiet hours)
MY = ("TSLA", "SPCX", "SMCI", "HOOD")
ELON = Account(handle="elonmusk", tickers=("TSLA", "SPCX"), expect_name="Elon Musk", user_id="44196397")
SUPERMICRO = Account(handle="Supermicro", tickers=("SMCI",), kind="company", expect_name="Supermicro",
                     user_id="374598930")
VLAD = Account(handle="vladtenev", tickers=("HOOD",), expect_name="Vlad Tenev", user_id="605700792")
WATCHLIST = Watchlist(
    accounts=(ELON, SUPERMICRO, VLAD),
    keyword_rules=(),
    my_tickers=MY,
    company_names={"TSLA": ("Tesla", "Cybertruck"), "SMCI": ("Supermicro",), "HOOD": ("Robinhood",)},
)


class Clock:
    def __init__(self, now: datetime = NOON) -> None:
        self.now = now

    def __call__(self) -> datetime:
        return self.now


def red_score(tickers: list[str], impact: int = 6, claim: str = "unverified", **kw: Any) -> Score:
    return Score(tickers=tickers, direction="bearish", impact=impact, reason="Bad news is claimed.", is_joke=False,
                 source="claude", red_flag=True, red_flag_types=["fraud_accounting"], claim_status=claim,  # type: ignore[arg-type]
                 false_news_risk="medium", **kw)


def post_at(author: str, at: datetime, n: int = 0, username: str | None = None, **kw: Any) -> Post:
    pid = str(datetime_to_snowflake(at) + n)
    return Post(id=pid, text="x", author_id=author, created_at=at, received_at=at, source="stream",
                author_username=username or f"user{author}", **kw)


CRED = Credibility(level="unverified")
OFFICIAL = Credibility(level="official", is_official=True)
TRUSTED = Credibility(level="trusted", is_trusted_source=True)


@pytest.fixture
def store() -> Iterator[Store]:
    s = Store(":memory:")
    yield s
    s.close()


def tracker(store: Store, clock: Clock | None = None, **settings: Any) -> RedFlagTracker:
    return RedFlagTracker(store, replace(RedFlagSettings(), **settings), MY, clock=clock or Clock())


# =========================================================================== spreading


def test_spreading_counts_distinct_authors_in_the_window(store: Store) -> None:
    clock = Clock()
    t = tracker(store, clock)
    s1 = t.observe(post_at("1", NOON - timedelta(minutes=50)), red_score(["SMCI"]), CRED)
    assert s1 is not None and s1.distinct_authors == 1 and not s1.spreading
    s2 = t.observe(post_at("1", NOON - timedelta(minutes=40), n=1), red_score(["SMCI"]), CRED)  # same author again
    assert s2 is not None and s2.distinct_authors == 1
    s3 = t.observe(post_at("2", NOON - timedelta(minutes=30), username="sample_wire"),
                   red_score(["SMCI"], claim="credible_report"), TRUSTED)
    assert s3 is not None and s3.distinct_authors == 2 and not s3.spreading and s3.confirmed_by == ["@sample_wire"]
    s4 = t.observe(post_at("3", NOON), red_score(["SMCI"]), CRED)
    assert s4 is not None and s4.spreading and s4.distinct_authors == 3 and s4.window_minutes == 60
    assert len(s4.related_post_ids) == 4
    # within the cooldown: still counted, but no second spreading alert
    s5 = t.observe(post_at("4", NOON + timedelta(minutes=1)), red_score(["SMCI"]), CRED)
    assert s5 is not None and s5.distinct_authors == 4 and not s5.spreading


def test_spreading_window_uses_post_time_and_cooldown_expires(store: Store) -> None:
    clock = Clock()
    t = tracker(store, clock, spread_min_authors=2, spread_cooldown_minutes=30)
    t.observe(post_at("1", NOON - timedelta(minutes=61)), red_score(["TSLA"]), CRED)  # outside the 60-min window
    s = t.observe(post_at("2", NOON), red_score(["TSLA"]), CRED)
    assert s is not None and s.distinct_authors == 1 and not s.spreading
    assert t.observe(post_at("3", NOON + timedelta(minutes=1)), red_score(["TSLA"]), CRED).spreading  # type: ignore[union-attr]
    clock.now = NOON + timedelta(minutes=10)
    assert not t.observe(post_at("4", NOON + timedelta(minutes=9)), red_score(["TSLA"]), CRED).spreading  # type: ignore[union-attr]
    clock.now = NOON + timedelta(minutes=45)  # cooldown over
    assert t.observe(post_at("5", NOON + timedelta(minutes=44)), red_score(["TSLA"]), CRED).spreading  # type: ignore[union-attr]


def test_release_spread_lets_the_next_post_retry(store: Store) -> None:
    t = tracker(store, spread_min_authors=2)
    t.observe(post_at("1", NOON), red_score(["TSLA"]), CRED)
    s = t.observe(post_at("2", NOON, n=1), red_score(["TSLA"]), CRED)
    assert s is not None and s.spreading
    t.release_spread("TSLA")
    assert t.observe(post_at("3", NOON, n=2), red_score(["TSLA"]), CRED).spreading  # type: ignore[union-attr]


def test_only_red_flags_about_my_tickers_are_tracked(store: Store) -> None:
    t = tracker(store)
    assert t.observe(post_at("1", NOON), replace(red_score(["TSLA"]), red_flag=False), CRED) is None
    assert t.observe(post_at("1", NOON), red_score(["XOM"]), CRED) is None
    assert t.observe(post_at("1", NOON), replace(red_score(["TSLA"]), is_joke=True), CRED) is None
    assert t.observe(post_at("1", NOON), red_score(["TSLA"], claim="denial"), CRED) is None
    assert t.observe(post_at("1", NOON), red_score(["TSLA"]), CRED) is not None
    assert tracker(store, enabled=False).observe(post_at("2", NOON), red_score(["TSLA"]), CRED) is None


def test_the_most_active_ticker_is_reported_and_spreading_wins(store: Store) -> None:
    t = tracker(store, spread_min_authors=2)
    t.observe(post_at("1", NOON), red_score(["SMCI"]), CRED)
    s = t.observe(post_at("2", NOON, n=1), red_score(["TSLA", "SMCI"]), CRED)
    assert s is not None and s.ticker == "SMCI" and s.spreading


def test_state_survives_a_restart(tmp_path: Path) -> None:
    db = tmp_path / "ceoscan.db"
    first = Store(db)
    t = tracker(first, spread_min_authors=2)
    t.observe(post_at("1", NOON), red_score(["TSLA"]), CRED)
    assert t.observe(post_at("2", NOON, n=1), red_score(["TSLA"]), CRED).spreading  # type: ignore[union-attr]
    first.close()
    second = Store(db)
    try:
        t2 = tracker(second, spread_min_authors=2)
        s = t2.observe(post_at("3", NOON, n=2), red_score(["TSLA"]), CRED)
        assert s is not None and s.distinct_authors == 3 and not s.spreading  # cooldown kept in kv
        assert second.kv_get(KV_SPREAD_PREFIX + "TSLA")
    finally:
        second.close()


def test_peek_is_a_hint_and_records_nothing(store: Store) -> None:
    t = tracker(store)
    assert t.peek(["TSLA"], NOON) is None
    t.observe(post_at("1", NOON - timedelta(minutes=5), username="sample_wire"),
              red_score(["TSLA"], claim="credible_report"), TRUSTED)
    hint = t.peek(["tsla", "$SMCI", "AAPL"], NOON)
    assert hint is not None and hint.ticker == "TSLA" and hint.distinct_authors == 1 and not hint.spreading
    assert hint.confirmed_by == ["@sample_wire"]
    assert len(store.red_flag_events("TSLA", NOON - timedelta(hours=1))) == 1


def test_old_events_are_pruned(store: Store) -> None:
    clock = Clock(NOON - timedelta(days=40))
    t = tracker(store, clock)
    t.observe(post_at("1", clock.now), red_score(["TSLA"]), CRED)
    clock.now = NOON
    t._last_prune = None
    t.observe(post_at("2", NOON), red_score(["TSLA"]), CRED)
    rows = store.red_flag_events("TSLA", NOON - timedelta(days=60))
    assert [r["author_id"] for r in rows] == ["2"]


# =========================================================================== denials


def denial_post(text: str, *, account: Account = ELON, refs: list[ReferencedPost] | None = None,
                at: datetime = NOON) -> Post:
    return Post(id=str(datetime_to_snowflake(at)), text=text, author_id=account.user_id or "1", created_at=at,
                received_at=at, source="stream", author_username=account.handle, referenced=refs or [])


def test_denial_links_back_once(store: Store) -> None:
    t = tracker(store)
    fake = post_at("666", NOON - timedelta(hours=2), username="elonmusk_ceo2")
    t.observe(fake, red_score(["TSLA"], claim="likely_false"), CRED)
    deny = denial_post("Fake. Tesla is not recalling the Cybertruck.",
                       refs=[ReferencedPost(type="replied_to", id=fake.id)])
    score = Score(["TSLA"], "unclear", 4, "denial", False, "claude", claim_status="denial")
    tickers = t.denial_tickers(deny, ELON, score, ["TSLA"])
    assert tickers == ["TSLA"]
    sig = t.find_denial(deny, tickers)
    assert sig is not None and sig.ticker == "TSLA" and sig.denied_by == ["@elonmusk"]
    assert sig.related_post_ids == [fake.id] and sig.window_minutes == 48 * 60
    again = denial_post("Again: fake news.", refs=[ReferencedPost(type="replied_to", id=fake.id)],
                        at=NOON + timedelta(minutes=5))
    assert t.find_denial(again, ["TSLA"]) is None  # already denied


def test_denial_needs_a_link_to_the_ticker(store: Store) -> None:
    t = tracker(store)
    t.observe(post_at("666", NOON - timedelta(hours=1)), red_score(["TSLA"]), CRED)
    unrelated = denial_post("False.", refs=[ReferencedPost(type="replied_to", id="123")])
    rules_score = Score([], "unclear", 1, "chit-chat", False, "rules_fallback")
    assert t.denial_tickers(unrelated, ELON, rules_score, []) == []  # a bare "False." to some other post
    assert t.denial_tickers(denial_post("Tesla is not recalling anything."), ELON, rules_score, ["TSLA"]) == ["TSLA"]
    company = denial_post("These reports are categorically false.", account=SUPERMICRO)
    assert t.denial_tickers(company, SUPERMICRO, rules_score, []) == ["SMCI"]  # a company denies for its tickers
    claude = Score(["TSLA"], "unclear", 3, "Denies a report", False, "claude", claim_status="denial")
    assert t.denial_tickers(denial_post("Nope, not happening."), ELON, claude, []) == ["TSLA"]
    assert t.denial_tickers(denial_post("Tesla cuts guidance"), ELON, rules_score, ["TSLA"]) == []
    assert t.denial_tickers(denial_post("False."), None, claude, ["TSLA"]) == []  # only watched accounts deny


def test_denial_ignores_own_and_old_red_flags(store: Store) -> None:
    t = tracker(store)
    t.observe(post_at(ELON.user_id or "", NOON - timedelta(hours=1), username="elonmusk"), red_score(["TSLA"]), OFFICIAL)
    t.observe(post_at("7", NOON - timedelta(hours=49)), red_score(["TSLA"]), CRED)  # beyond the 48 h lookback
    assert t.find_denial(denial_post("Fake news."), ["TSLA"]) is None


def test_compromise_cooldown(store: Store) -> None:
    clock = Clock()
    t = tracker(store, clock)
    assert t.claim_compromise("605700792") is True
    assert t.claim_compromise("605700792") is False
    assert t.claim_compromise("44196397") is True
    clock.now = NOON + timedelta(minutes=181)
    assert t.claim_compromise("605700792") is True


# =========================================================================== store


def test_old_database_is_migrated(tmp_path: Path) -> None:
    db = tmp_path / "old.db"
    con = sqlite3.connect(db)
    con.executescript(
        """CREATE TABLE alerts (id INTEGER PRIMARY KEY AUTOINCREMENT, post_id TEXT, kind TEXT NOT NULL,
           created_at_ms INTEGER NOT NULL, decision_reason TEXT, score INTEGER, direction TEXT, tickers TEXT,
           title TEXT, body TEXT, channels TEXT, sent_ok INTEGER NOT NULL, quiet_hours INTEGER NOT NULL DEFAULT 0,
           total_latency_ms INTEGER, x_delay_ms INTEGER, scoring_ms INTEGER, push_ms INTEGER);
           INSERT INTO alerts (post_id, kind, created_at_ms, sent_ok) VALUES ('1', 'post', 1, 1);"""
    )
    con.close()
    store = Store(db)
    try:
        cols = store._columns("alerts")
        assert {"late", "red_flag_kind", "claim_status"} <= cols
        assert store._db.execute("SELECT red_flag_kind FROM alerts").fetchone()[0] == ""
        assert "red_flag_events" in {r[0] for r in store._db.execute("SELECT name FROM sqlite_master")}
        rec = AlertRecord(post_id="2", kind="post", decision_reason="r", message=AlertMessage("t", "b"),
                          results=[ChannelResult("capture", True, 5)], red_flag_kind="spreading",
                          claim_status="unverified")
        store.record_alert(rec)
        row = store._db.execute("SELECT red_flag_kind, claim_status FROM alerts WHERE post_id='2'").fetchone()
        assert tuple(row) == ("spreading", "unverified")
    finally:
        store.close()
    Store(db).close()  # idempotent


def _record(store: Store, post_id: str, kind: str, claim: str | None, ok: bool = True) -> None:
    store.record_alert(AlertRecord(post_id=post_id, kind="post", decision_reason="r", message=AlertMessage("t", "b"),
                                   results=[ChannelResult("capture", ok, 5)], score=6, tickers=["TSLA"],
                                   red_flag_kind=kind, claim_status=claim))  # type: ignore[arg-type]


def test_stats_count_red_flags(store: Store) -> None:
    _record(store, "1", "red_flag", "unverified")
    _record(store, "2", "red_flag", "credible_report")
    _record(store, "3", "spreading", "likely_false")
    _record(store, "4", "denial", "denial")
    _record(store, "5", "compromise", "likely_false")
    _record(store, "6", "", None)
    _record(store, "7", "red_flag", "unverified", ok=False)  # not delivered
    t = tracker(store)
    t.observe(post_at("1", NOON), red_score(["TSLA", "SMCI"]), CRED)  # one post, two tickers
    stats = store.stats_since(NOON - timedelta(days=30))
    assert (stats["red_flags"], stats["red_flags_unverified"], stats["spreading"], stats["denials"],
            stats["compromises"], stats["red_flag_posts"]) == (3, 2, 1, 1, 1, 1)
    assert stats["alerts"] == 6
    rows = store.post_alerts_since(NOON - timedelta(days=30))
    assert {r["post_id"]: (r["red_flag_kind"], r["claim_status"]) for r in rows}["3"] == ("spreading", "likely_false")
    assert {r["post_id"]: r["red_flag_kind"] for r in rows}["6"] == ""


# =========================================================================== pipeline


def sid(at: datetime = NOON, seconds_before: float = 5.0, n: int = 0) -> str:
    return str(datetime_to_snowflake(at - timedelta(seconds=seconds_before)) + n)


def user(uid: str, username: str, name: str = "Someone", followers: int = 20_000, vtype: str = "none",
         created: str = "2015-01-01T00:00:00.000Z") -> dict[str, Any]:
    return {"id": uid, "username": username, "name": name, "verified": vtype != "none", "verified_type": vtype,
            "created_at": created, "public_metrics": {"followers_count": followers}}


def payload(pid: str, text: str, author: dict[str, Any], *, refs: list[dict[str, Any]] | None = None,
            tweets: list[dict[str, Any]] | None = None, tag: str = "ceoscan:kw:red-flags:1") -> dict[str, Any]:
    data: dict[str, Any] = {"id": pid, "text": text, "author_id": author["id"], "edit_history_tweet_ids": [pid]}
    if refs:
        data["referenced_tweets"] = refs
    includes: dict[str, Any] = {"users": [author]}
    if tweets:
        includes["tweets"] = tweets
    return {"data": data, "includes": includes, "matching_rules": [{"id": "1", "tag": tag}]}


class RedFlagScorer:
    """Scripted scorer that takes the red-flag keyword arguments: a red flag for each text in ``red``."""

    def __init__(self, red: dict[str, int] | None = None, default: int = 3) -> None:
        self.red = red or {}
        self.default = default
        self.calls: list[dict[str, Any]] = []

    async def score(self, post: Post, account: Account | None, rules: RuleResult, my: tuple[str, ...], tz: str,
                    *, credibility: Credibility | None = None, signal: RedFlagSignal | None = None) -> Score:
        self.calls.append({"post": post, "credibility": credibility, "signal": signal})
        tickers = list(rules.tickers) or (list(account.tickers) if account else [])
        for needle, impact in self.red.items():
            if needle in post.text:
                claim = {"official": "confirmed_official", "trusted": "credible_report"}.get(
                    credibility.level if credibility else "", "unverified")
                return red_score(tickers, impact, claim)
        claim = "denial" if "fake" in post.text.lower() or "false" in post.text.lower() else "not_applicable"
        return Score(tickers, "unclear", self.default, "scripted", False, "claude", claim_status=claim)  # type: ignore[arg-type]


class FakeAnalyst:
    """Stands in for the Claude desk / Duet feed: records wants() and submit()."""

    def __init__(self) -> None:
        self.wants_calls: list[str] = []
        self.submitted: list[AlertRecord] = []

    def wants(self, score: Score | None, account: Account | None, late: bool, *, red_flag_kind: str = "") -> bool:
        self.wants_calls.append(red_flag_kind)
        return bool(red_flag_kind)

    def submit(self, rec: AlertRecord, post: Post, score: Score | None, account: Account | None,
               rules: RuleResult | None = None) -> bool:
        self.submitted.append(rec)
        return True


class Env:
    def __init__(self, *, clock: datetime = NOON, settings: Settings | None = None, scorer: Any = None,
                 ok: bool = True, sms: bool = False) -> None:
        self.settings = settings or Settings()
        self.cfg = AppConfig(self.settings, WATCHLIST, Secrets(), Path("s.yaml"), Path("w.yaml"))
        self.store = Store(":memory:")
        self.capture = CaptureNotifier(ok=ok)
        self.sms = CaptureNotifier(ok=True) if sms else None
        self.dispatcher = AlertDispatcher(self.settings.alerts, push=[self.capture], sms=self.sms)
        self.scorer = scorer or RedFlagScorer()
        self.clock = Clock(clock)
        self.desk, self.duet = FakeAnalyst(), FakeAnalyst()
        self.pipeline = Pipeline(self.cfg, self.store, self.scorer, self.dispatcher, IngestStatus(mode="stream"),
                                 clock=self.clock, desk=self.desk, duet=self.duet)  # type: ignore[arg-type]

    async def run(self, p: dict[str, Any]) -> Any:
        return await self.pipeline.process(IngestItem(payload=p, received_at=self.clock.now - timedelta(seconds=2),
                                                      source="stream"))


@pytest.fixture
def env() -> Iterator[Env]:
    e = Env()
    yield e
    e.store.close()


SHORT = user("9001", "sample_shortco", "Sample Short Research", 185_000, "blue")
WIRE = user("9002", "sample_wire", "Sample Wire", 3_100_000, "business")
FLASH = user("9003", "sample_flash", "Market Flash News", 12_400, "none", created="2026-11-01T00:00:00.000Z")
FAKE_ELON = user("9004", "elonmusk_ceo2", "Elon Musk", 15_800, "blue", created="2026-11-05T00:00:00.000Z")


async def test_red_flag_post_alerts_at_min_score_with_badge(env: Env) -> None:
    env.scorer.red = {"short report": 6}
    res = await env.run(payload(sid(), "We are short $SMCI: our short report alleges fraud at Supermicro.", SHORT))
    assert res.outcome == "alert" and res.red_flag is not None and res.red_flag.kind == "red_flag"
    rec = res.alert
    assert rec.red_flag_kind == "red_flag" and rec.claim_status == "unverified"
    assert rec.credibility is not None and rec.credibility.level == "established"
    assert rec.red_flag_signal is not None and rec.red_flag_signal.distinct_authors == 1
    assert env.capture.sent[0].title == "🚩 RED FLAG $SMCI 6/10 bearish - UNVERIFIED (could be false)"
    # the scorer got the credibility; the desk / Duet were asked with the red-flag kind and got the record
    assert env.scorer.calls[0]["credibility"].level == "established"
    assert env.desk.wants_calls == ["red_flag"] and env.duet.wants_calls == ["red_flag"]
    assert env.desk.submitted == [rec] and env.duet.submitted == [rec]
    row = env.store._db.execute("SELECT red_flag_kind, claim_status FROM alerts").fetchone()
    assert tuple(row) == ("red_flag", "unverified")


async def test_ordinary_post_keeps_the_normal_threshold(env: Env) -> None:
    env.scorer.default = 6
    res = await env.run(payload(sid(), "Supermicro ships new servers. $SMCI", WIRE))
    assert res.outcome == "no_alert" and "below threshold: 6 < 7" in res.detail and res.red_flag is None
    assert env.desk.wants_calls == [] and env.desk.submitted == []


async def test_scorer_hint_counts_bad_news_so_far(env: Env) -> None:
    env.scorer.red = {"fraud": 6}
    await env.run(payload(sid(n=1), "Supermicro fraud alleged. $SMCI", SHORT))
    await env.run(payload(sid(n=2), "Supermicro fraud? $SMCI", WIRE))
    hint = env.scorer.calls[1]["signal"]
    assert hint is not None and hint.ticker == "SMCI" and hint.distinct_authors == 1


async def test_spreading_alert_once_per_cooldown(env: Env) -> None:
    env.scorer.red = {"fraud": 6, "rumor": 3}  # the rumor alone is below red_flags.min_score
    await env.run(payload(sid(n=1), "Short report: fraud at Supermicro. $SMCI", SHORT))
    await env.run(payload(sid(n=2), "Supermicro auditor resigns amid fraud questions. $SMCI", WIRE))
    res = await env.run(payload(sid(n=3), "rumor: SEC probing Supermicro. $SMCI", FLASH))
    assert res.outcome == "alert" and res.red_flag.kind == "spreading"
    rec = res.alert
    assert rec.red_flag_kind == "spreading" and rec.score == 6 and rec.direction == "bearish"
    assert rec.tickers[0] == "SMCI"
    assert rec.red_flag_signal.distinct_authors == 3 and rec.message.priority == "high"
    # the strongest claim in the window is the wire's CREDIBLE report (SW-6)
    assert rec.message.title == "🚩 Bad news spreading: $SMCI - 3 accounts in 60 min (incl. CREDIBLE)"
    assert rec.claim_status == "credible_report"
    assert "@sample_shortco" in rec.message.body and "(alerted)" in rec.message.body
    assert env.desk.submitted[-1] is rec and env.duet.submitted[-1] is rec
    # a 4th account within the cooldown: an ordinary red flag (if it qualifies), not another spreading alert
    fourth = await env.run(payload(sid(n=4), "rumor again: Supermicro. $SMCI", user("9009", "another", "A")))
    assert fourth.outcome == "no_alert" and fourth.red_flag.kind == "red_flag"
    assert [a["red_flag_kind"] for a in env.store._db.execute("SELECT red_flag_kind FROM alerts")].count("spreading") == 1


async def test_undelivered_spreading_alert_is_retried() -> None:
    e = Env(ok=False, settings=replace(Settings(), red_flags=replace(RedFlagSettings(), spread_min_authors=2)))
    try:
        e.scorer.red = {"fraud": 6}
        await e.run(payload(sid(n=1), "Supermicro fraud. $SMCI", SHORT))
        first = await e.run(payload(sid(n=2), "Supermicro fraud. $SMCI", WIRE))
        assert first.red_flag.kind == "spreading" and not first.alert.sent_ok
        e.capture.ok = True
        retry = await e.run(payload(sid(n=3), "Supermicro fraud. $SMCI", FLASH))
        assert retry.red_flag.kind == "spreading" and retry.alert.sent_ok
    finally:
        e.store.close()


async def test_denial_by_the_watched_account(env: Env) -> None:
    env.scorer.red = {"recalling every": 6}
    fake_id = sid(n=1)
    first = await env.run(payload(fake_id, "Tesla is recalling every Cybertruck. $TSLA", FAKE_ELON))
    assert first.outcome == "alert" and first.credibility.possible_impersonation
    deny = payload(sid(n=2), "@elonmusk_ceo2 Fake account. Tesla is not recalling the Cybertruck.",
                   user(ELON.user_id or "", "elonmusk", "Elon Musk", 220_000_000, "blue"),
                   refs=[{"type": "replied_to", "id": fake_id}], tag="ceoscan:acct:r1t0:1")
    res = await env.run(deny)
    assert res.outcome == "alert" and res.red_flag.kind == "denial"
    rec = res.alert
    assert rec.red_flag_kind == "denial" and rec.red_flag_signal.denied_by == ["@elonmusk"]
    assert rec.red_flag_signal.related_post_ids == [fake_id]
    assert rec.message.title == "🛡️ DENIED by @elonmusk: $TSLA"
    assert f"https://x.com/elonmusk_ceo2/status/{fake_id}" in rec.message.body
    assert env.desk.submitted[-1] is rec


async def test_hijacked_account_alerts_through_quiet_hours() -> None:
    e = Env(clock=NIGHT, sms=True)
    try:
        e.scorer.default = 9  # Claude might even call the "token launch" bullish
        text = "Robinhood is launching our own token $HOOD! Claim your airdrop: SynthetiCFakeAddressNotReaL2925demoXpump"
        vlad = user(VLAD.user_id or "", "vladtenev", "Vlad Tenev", 300_000, "blue")
        res = await e.run(payload(sid(NIGHT, n=1), text, vlad, tag="ceoscan:acct:r1t0:1"))
        assert res.outcome == "alert" and res.red_flag.kind == "compromise"
        rec = res.alert
        assert rec.message.title == "⚠️ @vladtenev may be HACKED - do not act on this post"
        assert rec.message.priority == "high" and rec.quiet_hours and rec.direction == "unclear"
        assert "🟢" not in rec.message.title  # never shown as a bullish alert
        again = await e.run(payload(sid(NIGHT, n=2), text + " 100x", vlad, tag="ceoscan:acct:r1t0:1"))
        assert again.red_flag.kind == "compromise" and again.alert.message.priority == "low"  # repeat in cooldown
    finally:
        e.store.close()


async def test_quiet_hours_red_flags_and_silent_spreading() -> None:
    settings = replace(Settings(), red_flags=replace(RedFlagSettings(), spread_min_authors=2))
    e = Env(clock=NIGHT, sms=True, settings=settings)
    try:
        e.scorer.red = {"fraud": 8}
        first = await e.run(payload(sid(NIGHT, n=1), "Supermicro fraud. $SMCI", SHORT))
        # 8 < 9 at night: delivered silently (M5), waiting on the phone at market open - not dropped
        assert first.outcome == "alert" and first.alert.message.priority == "low"
        assert "quiet hours: silent" in first.detail
        second = await e.run(payload(sid(NIGHT, n=2), "Supermicro fraud. $SMCI", WIRE))
        assert second.red_flag.kind == "spreading" and second.alert.message.priority == "low"
        assert "silent" in second.alert.decision_reason
        assert e.sms is not None and e.sms.sent == []  # no SMS overnight although the score is 8
    finally:
        e.store.close()


async def test_red_flags_off_changes_nothing() -> None:
    settings = replace(Settings(), red_flags=replace(RedFlagSettings(), enabled=False))
    e = Env(settings=settings)
    try:
        e.scorer.red = {"fraud": 6}
        res = await e.run(payload(sid(), "Supermicro fraud. $SMCI", SHORT))
        assert res.outcome == "no_alert" and res.red_flag is None and res.credibility is None
        assert e.scorer.calls[0]["credibility"] is None
        text = "Launching our own token $HOOD! Claim your airdrop SynthetiCFakeAddressNotReaL2925demoXpump"
        hijack = await e.run(payload(sid(n=2), text, user(VLAD.user_id or "", "vladtenev"), tag="ceoscan:acct:r1t0:1"))
        assert hijack.red_flag is None
    finally:
        e.store.close()


async def test_scorer_without_red_flag_parameters_still_works(env: Env) -> None:
    class Plain:
        async def score(self, post: Post, account: Account | None, rules: RuleResult, my: tuple[str, ...],
                        tz: str) -> Score:
            return red_score(list(rules.tickers), 7)

    e = Env(scorer=Plain())
    try:
        res = await e.run(payload(sid(), "Supermicro fraud. $SMCI", SHORT))
        assert res.outcome == "alert" and res.alert.red_flag_kind == "red_flag"
    finally:
        e.store.close()


# =========================================================================== /health and the daily summary


def test_health_and_summary_show_red_flags(store: Store) -> None:
    _record(store, "1", "red_flag", "unverified")
    _record(store, "2", "spreading", "unverified")
    _record(store, "3", "denial", "denial")
    _record(store, "4", "compromise", "likely_false")
    settings = Settings()
    now = datetime.now(UTC)
    health = HealthServer("127.0.0.1", 0, IngestStatus(), store, settings, now)
    today = health.snapshot(now)["today"]
    assert (today["red_flags"], today["red_flags_unverified"], today["spreading"], today["denials"],
            today["compromises"]) == (2, 2, 1, 1, 1)
    cfg = AppConfig(settings, WATCHLIST, Secrets(), Path("s.yaml"), Path("w.yaml"))
    summary = OpsScheduler(cfg, store, IngestStatus(), AlertDispatcher(settings.alerts, push=[])).build_summary(now)
    assert "Red flags: 2 (2 unverified, 1 spreading), 1 denied, 1 possible hacked account" in summary.body
    assert ", 2 red flags" in (summary.sms_text or "")
