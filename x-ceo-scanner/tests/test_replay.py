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
    kw = [s for s in samples if any(":kw:" in r["tag"] for r in s["matching_rules"])]
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
    for handle in ("@elonmusk", "@vladtenev", "@saylor", "@satyanadella"):
        assert handle in titles

    # latency is measured on rebased (fresh) ids: ~x_delay_seconds (3 s) post -> console
    for a in alerts:
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

    assert "no alert (not flagged)" in line_for("sama")  # chit-chat
    assert "no alert" in line_for("sample_pumper")  # prompt injection gets no boost
    assert "ALERT" in line_for("sample_macro")  # keyword-rule post from a big non-watchlist account
    assert "ALERT" in line_for("satyanadella")  # flagged only through the quoted post
    assert any("edit of post" in line for line in lines)  # edited duplicate of an alerted post
    assert len({a.post_id for a in alerts}) == len(alerts)


async def test_run_replay_keep_times_uses_original_ids(cfg: AppConfig, no_network: None) -> None:
    alerts = await run_replay(cfg, SAMPLES, rebase_times=False, out=lambda _line: None)
    assert alerts
    assert {a.post_id for a in alerts} <= set(sample_ids())
    assert all(a.total_latency_ms is not None and a.total_latency_ms > 10_000 for a in alerts)


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
