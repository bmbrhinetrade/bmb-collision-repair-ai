"""Replay: sample loading, time rebasing, and the full pipeline over samples/sample_posts.jsonl (no X)."""

from __future__ import annotations

import json
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

import ceoscan.replay as replay_mod
from ceoscan.alerts.dispatcher import AlertDispatcher
from ceoscan.alerts.notifiers import CaptureNotifier
from ceoscan.config import AppConfig, Secrets, Settings, load_settings, load_watchlist
from ceoscan.models import AlertMessage
from ceoscan.replay import (
    AGE_KEY,
    REPLAY_TAG,
    ReplayDispatcher,
    TimeRebaser,
    load_samples,
    run_replay,
)
from ceoscan.store import Store
from ceoscan.timeutil import snowflake_to_datetime

ROOT = Path(__file__).resolve().parents[1]
SAMPLES = ROOT / "samples" / "sample_posts.jsonl"


@pytest.fixture
def cfg() -> AppConfig:
    """The real settings + watchlist, with quiet hours off so the result does not depend on the time of day."""
    settings = load_settings(ROOT / "config" / "settings.yaml")
    settings.alerts.quiet_hours.enabled = False
    return AppConfig(
        settings=settings,
        watchlist=load_watchlist(ROOT / "config" / "watchlist.yaml"),
        secrets=Secrets(),
        settings_path=ROOT / "config" / "settings.yaml",
        watchlist_path=ROOT / "config" / "watchlist.yaml",
    )


@pytest.fixture
def no_network(monkeypatch: pytest.MonkeyPatch) -> None:
    """Replay without --send must never open an HTTP session (X or anything else)."""

    def boom(*args: Any, **kwargs: Any) -> None:
        raise AssertionError("replay opened an aiohttp session")

    monkeypatch.setattr(replay_mod.aiohttp, "ClientSession", boom)


def sample_ids() -> list[str]:
    return [s["data"]["id"] for s in load_samples(SAMPLES)]


def by_handle(samples: list[dict[str, Any]], handle: str) -> list[dict[str, Any]]:
    out = []
    for s in samples:
        users = {u["id"]: u["username"] for u in s.get("includes", {}).get("users", [])}
        if users.get(s["data"]["author_id"]) == handle:
            out.append(s)
    return out


# --------------------------------------------------------------------------- load_samples


def test_load_samples_skips_comments_and_blank_lines(tmp_path: Path) -> None:
    f = tmp_path / "s.jsonl"
    f.write_text('// header comment\n\n  // indented comment\n{"data": {"id": "1"}}\n   \n{"data": {"id": "2"}}\n',
                 encoding="utf-8")
    assert load_samples(f) == [{"data": {"id": "1"}}, {"data": {"id": "2"}}]


def test_load_samples_reports_bad_line_number(tmp_path: Path) -> None:
    f = tmp_path / "s.jsonl"
    f.write_text('// c\n{"data": {}}\n{not json\n', encoding="utf-8")
    with pytest.raises(ValueError, match=r"s\.jsonl:3: invalid JSON"):
        load_samples(f)


def test_load_samples_rejects_non_objects(tmp_path: Path) -> None:
    f = tmp_path / "s.jsonl"
    f.write_text("[1, 2]\n", encoding="utf-8")
    with pytest.raises(ValueError, match="expected a JSON object"):
        load_samples(f)


# --------------------------------------------------------------------------- the sample file


def test_sample_file_is_labelled_synthetic() -> None:
    lines = SAMPLES.read_text(encoding="utf-8").splitlines()
    assert lines[0].startswith("//")
    assert "SYNTHETIC" in lines[0] and "not real" in lines[0].lower()
    samples = load_samples(SAMPLES)
    assert all(s.get("synthetic") is True for s in samples)


def test_sample_file_covers_required_scenarios(cfg: AppConfig) -> None:
    samples = load_samples(SAMPLES)
    assert len(samples) >= 12
    ids = [s["data"]["id"] for s in samples]
    assert len(ids) - len(set(ids)) >= 1, "needs a duplicate id"

    datas = [s["data"] for s in samples]
    ref_types = {r["type"] for d in datas for r in d.get("referenced_tweets", [])}
    assert {"quoted", "replied_to"} <= ref_types
    assert any("note_tweet" in d and len(d["note_tweet"]["text"]) > 280 for d in datas)
    assert any("ignore previous instructions" in d["text"].lower() for d in datas)
    assert any("BTC" in d["text"] for d in datas)
    assert any(len(d.get("edit_history_tweet_ids", [])) > 1 for d in datas)

    handles = {a.handle_lower for a in cfg.watchlist.accounts}
    # the big-account cashtag rule (min_followers 1M); red-flag rule posts come from smaller accounts on purpose
    kw = [s for s in samples if any(":kw:big-account-cashtags:" in r["tag"] for r in s["matching_rules"])]
    assert kw, "needs a keyword-rule post"
    for s in kw:
        author = next(u for u in s["includes"]["users"] if u["id"] == s["data"]["author_id"])
        assert author["username"].lower() not in handles
        assert author["public_metrics"]["followers_count"] >= 1_000_000

    # every watchlist author in the samples really is on the watchlist
    for s in samples:
        tag = s["matching_rules"][0]["tag"]
        if ":acct:" in tag:
            author = next(u for u in s["includes"]["users"] if u["id"] == s["data"]["author_id"])
            assert author["username"].lower() in handles


# --------------------------------------------------------------------------- rebasing


def test_time_rebaser_maps_ids_consistently() -> None:
    rb = TimeRebaser()
    t0 = datetime(2026, 11, 19, 20, 0, tzinfo=UTC)
    a = {"data": {"id": "1000", "created_at": "2020-01-01T00:00:00.000Z", "conversation_id": "1000",
                  "edit_history_tweet_ids": ["1000"]}}
    b = {"data": {"id": "2000", "edit_history_tweet_ids": ["1000", "2000"], "conversation_id": "1000",
                  "referenced_tweets": [{"type": "quoted", "id": "1000"}]},
         "includes": {"tweets": [{"id": "1000", "text": "orig"}]}}

    ra = rb.rebase(a, t0)
    rb2 = rb.rebase(b, t0)  # same ms: still strictly increasing
    ra_again = rb.rebase(a, t0 + timedelta(seconds=5))  # duplicate keeps the first mapping

    new_a, new_b = ra["data"]["id"], rb2["data"]["id"]
    assert snowflake_to_datetime(new_a) == t0
    assert int(new_b) > int(new_a)
    assert ra_again["data"]["id"] == new_a
    assert ra["data"]["conversation_id"] == new_a and ra["data"]["edit_history_tweet_ids"] == [new_a]
    assert ra["data"]["created_at"] == "2026-11-19T20:00:00.000Z"
    assert rb2["data"]["edit_history_tweet_ids"] == [new_a, new_b]
    assert rb2["data"]["referenced_tweets"][0]["id"] == new_a
    assert rb2["includes"]["tweets"][0]["id"] == new_a
    assert a["data"]["id"] == "1000"  # input not mutated


def test_time_rebaser_back_dated_ids_keep_their_time() -> None:
    rb = TimeRebaser()
    now = datetime(2026, 11, 19, 20, 0, tzinfo=UTC)
    fresh = rb.rebase({"data": {"id": "1"}}, now)
    old = rb.rebase({"data": {"id": "2"}}, now - timedelta(hours=2), in_order=False)
    twin = rb.rebase({"data": {"id": "3"}}, now - timedelta(hours=2), in_order=False)
    after = rb.rebase({"data": {"id": "4"}}, now)
    assert snowflake_to_datetime(old["data"]["id"]) == now - timedelta(hours=2)
    assert old["data"]["id"] != twin["data"]["id"]  # same ms: still unique
    assert int(after["data"]["id"]) > int(fresh["data"]["id"])  # in-order ids unaffected


def test_time_rebaser_leaves_malformed_payloads_alone() -> None:
    rb = TimeRebaser()
    t0 = datetime(2026, 11, 19, tzinfo=UTC)
    assert rb.rebase({"errors": []}, t0) == {"errors": []}
    assert rb.rebase({"data": {"id": "abc"}}, t0) == {"data": {"id": "abc"}}


# --------------------------------------------------------------------------- run_replay


async def test_run_replay_produces_alerts_and_dedupes(cfg: AppConfig, tmp_path: Path, no_network: None) -> None:
    db = tmp_path / "replay.db"
    out: list[str] = []
    alerts = await run_replay(cfg, SAMPLES, db_path=str(db), out=out.append)

    samples = load_samples(SAMPLES)
    unique_ids = len(set(sample_ids()))
    assert len(alerts) >= 5
    assert all(a.sent_ok for a in alerts)
    assert all(a.message.title.startswith(REPLAY_TAG) for a in alerts)
    titles = " ".join(a.message.title for a in alerts)
    for handle in ("@elonmusk", "@vladtenev", "@saylor"):
        assert handle in titles
    # offline scores are rules-only: capped (never urgent) and labelled as such (a possible hijack is detected in
    # code whatever scored it, so its title carries no rules-only marker)
    assert all(a.score is not None and a.score <= cfg.settings.scoring.fallback_max_impact for a in alerts)
    assert all("(rules-only)" in a.message.title for a in alerts if a.red_flag_kind != "compromise")

    # latency is measured on rebased (fresh) ids: ~x_delay_seconds (3 s) post -> console
    live = [a for a in alerts if not a.late]
    assert len(live) >= 5
    for a in live:
        assert a.x_delay_ms is not None and 2900 <= a.x_delay_ms <= 4000
        assert a.total_latency_ms is not None and 2900 <= a.total_latency_ms < 10_000
        assert a.scoring_ms is not None and a.push_ms is not None

    # the duplicate sample is dropped, every other post and every alert is stored
    assert any("duplicate" in line and "ignored (dedupe)" in line for line in out)
    store = Store(db)
    try:
        assert store._db.execute("SELECT COUNT(*) FROM posts").fetchone()[0] == unique_ids
        assert store._db.execute("SELECT COUNT(*) FROM alerts").fetchone()[0] == len(alerts)
        assert store._db.execute("SELECT COUNT(*) FROM since_ids").fetchone()[0] == 0
        titles_in_db = [r[0] for r in store._db.execute("SELECT title FROM alerts").fetchall()]
        assert titles_in_db and all(t.startswith(REPLAY_TAG) for t in titles_in_db)
    finally:
        store.close()

    # one line per sample + summary
    numbered = [line for line in out if line.startswith("[")]
    assert len(numbered) == len(samples)
    summary = next(line for line in out if line.startswith("Replay summary:"))
    assert f"{len(samples)} samples" in summary and "1 duplicate" in summary
    assert f"{len(alerts)} alerts ({len(alerts)} delivered)" in summary
    assert any("X API was not contacted" in line for line in out)


async def test_run_replay_scenarios_offline(cfg: AppConfig, no_network: None) -> None:
    out: list[str] = []
    alerts = await run_replay(cfg, SAMPLES, out=out.append)
    lines = [line for line in out if line.startswith("[")]

    def line_for(handle: str, needle: str = "") -> str:
        return next(line for line in lines if f"@{handle} " in line and needle in line)

    # chit-chat from a watched account: not flagged, still scored (score_watchlist_unflagged), no alert
    sama = line_for("sama")
    assert " -       score " in sama and "no alert (below threshold" in sama
    assert "no alert" in line_for("sample_pumper")  # prompt injection gets no boost
    assert "ALERT" in line_for("sample_macro")  # keyword-rule post from a big non-watchlist account
    # flagged only through the quoted post; a rules-only score is based on the post's own text, so
    # offline it does not alert (Claude, which reads the quoted post, decides on the real run)
    assert " flagged " in line_for("satyanadella") and "score" in line_for("satyanadella")
    assert any("edit of post" in line for line in lines)  # edited duplicate of an alerted post
    assert len({a.post_id for a in alerts}) == len(alerts)
    # a watched account with no keyword hit at all is still scored
    assert " -       score " in line_for("LisaSu")


def test_sample_file_covers_red_flag_scenarios(cfg: AppConfig) -> None:
    """Short report, credible wire, new-account rumor, CEO impersonator, denial, hijacked account, anti-fraud ad."""
    samples = load_samples(SAMPLES)
    texts = " ".join(s["data"]["text"] for s in samples)
    for needle in ("short report", "auditor has resigned", "BREAKING!!!", "recalling every Cybertruck",
                   "Tesla is not recalling", "launching our own token", "block $40B in fraud"):
        assert needle in texts, needle
    users = {u["username"]: u for s in samples for u in s["includes"]["users"]}
    assert users["elonmusk_ceo2"]["name"] == "Elon Musk"  # the impersonator
    assert users["sample_wire"]["verified_type"] == "business"
    denial = next(s for s in samples if "Tesla is not recalling" in s["data"]["text"])
    fake = next(s for s in samples if "recalling every Cybertruck" in s["data"]["text"])
    assert denial["data"]["referenced_tweets"] == [{"type": "replied_to", "id": fake["data"]["id"]}]
    # the three $SMCI red flags fall inside one 60-minute window even with the original ids (keep-times replay)
    smci = [snowflake_to_datetime(s["data"]["id"]) for s in samples if "$SMCI" in s["data"]["text"]]
    assert len(smci) == 3 and max(smci) - min(smci) < timedelta(minutes=cfg.settings.red_flags.spread_window_minutes)


async def test_run_replay_red_flag_scenarios_offline(cfg: AppConfig, no_network: None) -> None:
    """The red-flag samples end to end, rules-only: badges, spreading, denial, hijack, and no false alarm."""
    out: list[str] = []
    alerts = await run_replay(cfg, SAMPLES, out=out.append)
    by_kind: dict[str, list[Any]] = {}
    for a in alerts:
        by_kind.setdefault(a.red_flag_kind, []).append(a)
    titles = [a.message.title for a in by_kind.get("red_flag", [])]
    assert any("RED FLAG $AMD" in t and "CONFIRMED (official)" in t for t in titles)  # sample 02: a guidance cut
    assert any("RED FLAG $SMCI" in t and "UNVERIFIED (could be false)" in t for t in titles)  # the short report
    assert any("RED FLAG $SMCI" in t and "CREDIBLE" in t for t in titles)  # the business-verified wire
    assert any("RED FLAG $TSLA" in t and "LIKELY FALSE" in t for t in titles)  # the CEO impersonator
    for a in by_kind["red_flag"]:
        assert a.direction == "bearish" and a.claim_status and a.credibility is not None

    (spread,) = by_kind["spreading"]
    assert spread.message.title.startswith(f"{REPLAY_TAG} 🚩 Bad news spreading: $SMCI - 3 accounts in 60 min")
    assert spread.red_flag_signal is not None and spread.red_flag_signal.distinct_authors == 3
    assert "@sample_shortco" in spread.message.body and "@sample_wire" in spread.message.body
    assert spread.message.priority == "high"

    (denial,) = by_kind["denial"]
    assert denial.message.title.startswith(f"{REPLAY_TAG} 🛡️ DENIED by @elonmusk: $TSLA")
    assert "https://x.com/elonmusk_ceo2/status/" in denial.message.body  # links back to the red flag

    (hacked,) = by_kind["compromise"]
    assert hacked.message.title == f"{REPLAY_TAG} ⚠️ @vladtenev may be HACKED - do not act on this post"
    assert hacked.message.priority == "high" and hacked.direction == "unclear"

    # anti-fraud marketing is not a red flag; the new account's rumor alone is below the bar (it spreads instead)
    lines = [line for line in out if line.startswith("[")]
    assert "no alert" in next(line for line in lines if "@sample_paynews" in line)
    assert "spreading" in next(line for line in lines if "@sample_flash " in line)


async def test_run_replay_keep_times_uses_original_ids(cfg: AppConfig, no_network: None) -> None:
    out: list[str] = []
    alerts = await run_replay(cfg, SAMPLES, rebase_times=False, out=out.append)
    assert alerts
    assert {a.post_id for a in alerts} <= set(sample_ids())
    assert all(a.total_latency_ms is not None and a.total_latency_ms > 10_000 for a in alerts)
    # the original sample times are long past: every alert is a late (catch-up) alert
    assert all(a.late and a.message.priority in ("low", "normal") for a in alerts)
    assert all("Missed during outage - posted" in a.message.title for a in alerts)
    assert not any(line.startswith("Latency post->phone") for line in out)  # late alerts are not latency
    assert any(line.startswith("Late (catch-up) alerts:") for line in out)


async def test_run_replay_back_dated_sample_is_a_late_alert(cfg: AppConfig, no_network: None) -> None:
    """The sample with replay_age_seconds replays an outage catch-up: low-key, flagged late, no latency."""
    samples = load_samples(SAMPLES)
    late_samples = [s for s in samples if AGE_KEY in s]
    assert late_samples, "needs a back-dated (outage catch-up) sample"
    out: list[str] = []
    alerts = await run_replay(cfg, SAMPLES, out=out.append)

    late = [a for a in alerts if a.late]
    assert len(late) == len(late_samples)
    for a in late:
        age_ms = late_samples[0][AGE_KEY] * 1000
        assert a.x_delay_ms is not None and abs(a.x_delay_ms - age_ms) < 2000
        assert a.message.title.startswith(f"{REPLAY_TAG} Missed during outage - posted 2h ago: ")
        assert a.message.priority in ("low", "normal")
        assert "late: posted 2h ago" in a.decision_reason
    assert any(line.startswith("[") and "LATE ALERT" in line for line in out)
    summary = next(line for line in out if line.startswith("Replay summary:"))
    assert f"| {len(late)} late" in summary
    latency = next(line for line in out if line.startswith("Latency post->phone"))
    assert "max 3." in latency  # the 2 h catch-up is not in the latency figures


async def test_run_replay_send_uses_real_dispatcher_and_marks_replay(
    cfg: AppConfig, monkeypatch: pytest.MonkeyPatch
) -> None:
    capture = CaptureNotifier()
    seen: dict[str, Any] = {}

    def fake_build(c: AppConfig, session: Any) -> AlertDispatcher:
        seen["session"] = session
        return AlertDispatcher(c.settings.alerts, push=[capture])

    monkeypatch.setattr(replay_mod, "build_dispatcher", fake_build)
    out: list[str] = []
    alerts = await run_replay(cfg, SAMPLES, send=True, out=out.append)

    assert alerts and len(capture.sent) == len(alerts)
    assert seen["session"].closed  # session closed afterwards
    for msg in capture.sent:
        assert msg.title.startswith(f"{REPLAY_TAG} ")
        assert (msg.sms_text or "").startswith(REPLAY_TAG)
        assert "not a live post" in msg.body
    assert any("real channels: capture" in line for line in out)


async def test_run_replay_use_claude_builds_claude_scorer(
    cfg: AppConfig, monkeypatch: pytest.MonkeyPatch, no_network: None
) -> None:
    created: list[Any] = []

    class FakeClaude(replay_mod.OfflineScorer):
        def __init__(self, settings: Any, api_key: str | None) -> None:
            super().__init__(settings)
            self.closed = False
            created.append(self)

        async def aclose(self) -> None:
            self.closed = True

    monkeypatch.setattr(replay_mod, "ClaudeScorer", FakeClaude)
    out: list[str] = []
    alerts = await run_replay(cfg, SAMPLES, use_claude=True, out=out.append)
    assert len(created) == 1 and created[0].closed
    assert alerts
    assert any("ANTHROPIC_API_KEY is not set" in line for line in out)
    assert any("Scorer: Claude" in line for line in out)


async def test_run_replay_matches_handles_even_after_verify(cfg: AppConfig, no_network: None) -> None:
    """After `verify --write` the watchlist has real user ids; fake sample ids still map by handle."""
    accounts = tuple(replace(a, user_id=str(10_000 + i)) for i, a in enumerate(cfg.watchlist.accounts))
    verified = replace(cfg, watchlist=replace(cfg.watchlist, accounts=accounts))
    alerts = await run_replay(verified, SAMPLES, out=lambda _line: None)
    assert any("@elonmusk" in a.message.title for a in alerts)


async def test_replay_dispatcher_marks_messages() -> None:
    capture = CaptureNotifier()
    d = ReplayDispatcher(AlertDispatcher(Settings().alerts, push=[capture]))
    results = await d.send(AlertMessage(title="T", body="B", sms_text="S"), 8)
    assert results[0].ok
    assert capture.sent[0].title == f"{REPLAY_TAG} T"
    assert capture.sent[0].sms_text == f"{REPLAY_TAG} S"
    assert d.channel_names == ["capture"]

    msg = AlertMessage(title="Only title", body="B")
    ReplayDispatcher.mark(msg)
    ReplayDispatcher.mark(msg)  # idempotent
    assert msg.title == f"{REPLAY_TAG} Only title"
    assert msg.sms_text == f"{REPLAY_TAG} Only title"
    assert msg.body.count("not a live post") == 1


def test_sample_json_lines_are_single_line_objects() -> None:
    for line in SAMPLES.read_text(encoding="utf-8").splitlines():
        if line.strip() and not line.startswith("//"):
            assert isinstance(json.loads(line), dict)


async def test_run_replay_explains_quiet_hours_with_rules_only_scores(
    cfg: AppConfig, monkeypatch: pytest.MonkeyPatch, no_network: None
) -> None:
    monkeypatch.setattr(replay_mod, "is_quiet_hours", lambda settings, now: True)
    out: list[str] = []
    await run_replay(cfg, SAMPLES, out=out.append)
    assert any(line.startswith("Note: quiet hours are active now") for line in out)
    assert any("capped at 7" in line and "--claude" in line for line in out)

    out.clear()
    await run_replay(cfg, SAMPLES, use_claude=True, out=out.append)  # no key: still notes quiet hours only
    assert any(line.startswith("Note: quiet hours are active now") for line in out)
    assert not any("capped at" in line for line in out)
