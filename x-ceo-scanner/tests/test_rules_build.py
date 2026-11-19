"""Unit tests for filtered-stream rule building, sync planning and syncing (xapi/rules.py)."""

from __future__ import annotations

import re
from collections import Counter
from dataclasses import replace
from itertools import pairwise
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import aiohttp
import aioresponses.core as aioresponses_core
import pytest
from aioresponses import CallbackResult, aioresponses

from ceoscan.config import RulesSettings, XApiSettings, load_watchlist
from ceoscan.models import Account, KeywordRule, Watchlist
from ceoscan.xapi.client import NotAuthorized, XClient
from ceoscan.xapi.rules import (
    RuleBuildResult,
    RuleSpec,
    RuleSyncPlan,
    account_suffix,
    build_rules,
    plan_sync,
    sync_rules,
)

ROOT = Path(__file__).resolve().parents[1]
RULES_URL = re.compile(r"^https://api\.x\.com/2/tweets/search/stream/rules(\?.*)?$")
FROM_RE = re.compile(r"from:(\d+)")
TAG_ACCT_RE = re.compile(r"^ceoscan:acct:r[01]t[01]:\d+$")
TAG_KW_RE = re.compile(r"^ceoscan:kw:[^:]+:\d+$")


class _CompatClientResponse(aiohttp.ClientResponse):
    """aioresponses 0.7.9 does not pass ``stream_writer`` (required by the pinned aiohttp 3.14)."""

    def __init__(self, method: str, url: Any, **kwargs: Any) -> None:
        kwargs.setdefault("stream_writer", SimpleNamespace(output_size=0))
        super().__init__(method, url, **kwargs)


@pytest.fixture(autouse=True)
def _aioresponses_compat(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(aioresponses_core, "ClientResponse", _CompatClientResponse)


def acct(
    handle: str,
    user_id: str | None,
    replies: bool = True,
    retweets: bool = False,
    tickers: tuple[str, ...] = ("TSLA",),
) -> Account:
    return Account(handle=handle, tickers=tickers, user_id=user_id, include_replies=replies, include_retweets=retweets)


def wl(accounts: list[Account], keyword_rules: list[KeywordRule] | None = None) -> Watchlist:
    my = tuple(dict.fromkeys(t for a in accounts for t in a.tickers))
    return Watchlist(
        accounts=tuple(accounts), keyword_rules=tuple(keyword_rules or ()), my_tickers=my, company_names={}
    )


def big_id(i: int) -> str:
    # realistic 19-digit snowflake-sized user ids
    return str(1_300_000_000_000_000_000 + i * 7919)


def acct_rules(result: RuleBuildResult) -> list[RuleSpec]:
    return [r for r in result.rules if ":acct:" in r.tag]


def kw_rules(result: RuleBuildResult) -> list[RuleSpec]:
    return [r for r in result.rules if ":kw:" in r.tag]


# --------------------------------------------------------------------------- account rules


@pytest.mark.parametrize("max_len", [60, 100, 257, 512, 1024])
def test_packing_never_exceeds_max_rule_length(max_len: int) -> None:
    accounts = [acct(f"user{i}", big_id(i), replies=i % 2 == 0, retweets=i % 3 == 0) for i in range(400)]
    kw = KeywordRule("big", tuple(f"T{i:03d}" for i in range(300)), 1_000_000, "-is:retweet -is:reply lang:en")
    res = build_rules(wl(accounts, [kw]), RulesSettings(max_rule_length=max_len, max_rules=10_000))
    assert res.rules
    for rule in res.rules:
        assert len(rule.value) <= max_len, rule
    # greedy packing: rules are not trivially one-id-each when there is room
    if max_len >= 512:
        assert len(acct_rules(res)) < len(accounts) / 10


def test_packing_is_greedy_adding_next_id_would_overflow() -> None:
    accounts = [acct(f"u{i}", big_id(i)) for i in range(200)]
    max_len = 300
    res = build_rules(wl(accounts), RulesSettings(max_rule_length=max_len))
    rules = acct_rules(res)
    assert len(rules) > 1
    ids_per_rule = [FROM_RE.findall(r.value) for r in rules]
    for chunk, nxt in pairwise(ids_per_rule):
        grown = "(" + " OR ".join(f"from:{i}" for i in [*chunk, nxt[0]]) + ") -is:retweet"
        assert len(grown) > max_len  # the next id genuinely did not fit


def test_every_user_id_appears_exactly_once_across_account_rules() -> None:
    accounts = [acct(f"u{i}", big_id(i), replies=i % 2 == 0, retweets=i % 5 == 0) for i in range(500)]
    res = build_rules(wl(accounts), RulesSettings(max_rule_length=200, max_rules=10_000))
    counts = Counter(uid for r in acct_rules(res) for uid in FROM_RE.findall(r.value))
    assert set(counts) == {a.user_id for a in accounts}
    assert set(counts.values()) == {1}


def test_duplicate_user_id_is_streamed_once_with_warning() -> None:
    accounts = [acct("old_handle", "111"), acct("new_handle", "111", replies=False), acct("other", "222")]
    res = build_rules(wl(accounts), RulesSettings())
    counts = Counter(uid for r in acct_rules(res) for uid in FROM_RE.findall(r.value))
    assert counts == {"111": 1, "222": 1}
    assert any("same user_id 111" in w for w in res.warnings)


def test_grouping_by_replies_and_retweets_suffixes() -> None:
    accounts = [
        acct("a", "1", replies=True, retweets=False),
        acct("b", "2", replies=False, retweets=False),
        acct("c", "3", replies=True, retweets=True),
        acct("d", "4", replies=False, retweets=True),
        acct("e", "5", replies=True, retweets=False),
        acct("f", "6", replies=False, retweets=False),
    ]
    res = build_rules(wl(accounts), RulesSettings())
    by_tag = {r.tag: r.value for r in res.rules}
    assert by_tag == {
        "ceoscan:acct:r1t0:1": "(from:1 OR from:5) -is:retweet",
        "ceoscan:acct:r0t0:1": "(from:2 OR from:6) -is:retweet -is:reply",
        "ceoscan:acct:r1t1:1": "from:3",
        "ceoscan:acct:r0t1:1": "from:4 -is:reply",
    }
    # group order follows first appearance in the watchlist
    assert [r.tag for r in res.rules] == [
        "ceoscan:acct:r1t0:1",
        "ceoscan:acct:r0t0:1",
        "ceoscan:acct:r1t1:1",
        "ceoscan:acct:r0t1:1",
    ]


@pytest.mark.parametrize(
    ("replies", "retweets", "suffix"),
    [
        (True, False, "-is:retweet"),
        (False, False, "-is:retweet -is:reply"),
        (True, True, ""),
        (False, True, "-is:reply"),
    ],
)
def test_account_suffix(replies: bool, retweets: bool, suffix: str) -> None:
    assert account_suffix(replies, retweets) == suffix


def test_suffix_matches_tag_for_every_packed_rule() -> None:
    accounts = [acct(f"u{i}", big_id(i), replies=i % 2 == 0, retweets=i % 3 == 0) for i in range(300)]
    res = build_rules(wl(accounts), RulesSettings(max_rule_length=150, max_rules=10_000))
    expected_group = {a.user_id: (a.include_replies, a.include_retweets) for a in accounts}
    for rule in acct_rules(res):
        m = re.match(r"^ceoscan:acct:r([01])t([01]):\d+$", rule.tag)
        assert m, rule.tag
        replies, retweets = bool(int(m.group(1))), bool(int(m.group(2)))
        suffix = account_suffix(replies, retweets)
        body = rule.value[: -len(suffix)].rstrip() if suffix else rule.value
        if suffix:
            assert rule.value.endswith(" " + suffix)
        assert "-is:" not in body
        for uid in FROM_RE.findall(rule.value):
            assert expected_group[uid] == (replies, retweets)


def test_accounts_without_user_id_are_skipped_and_reported() -> None:
    accounts = [acct("verified1", "101"), acct("unverified", None), acct("verified2", "202"), acct("nope", None)]
    res = build_rules(wl(accounts), RulesSettings())
    assert res.skipped_accounts == ["unverified", "nope"]
    values = " ".join(r.value for r in res.rules)
    assert "unverified" not in values and "nope" not in values
    assert FROM_RE.findall(values) == ["101", "202"]
    assert any("verify --write" in w and "@unverified" in w for w in res.warnings)


def test_no_verified_accounts_and_no_keywords_gives_no_rules() -> None:
    res = build_rules(wl([acct("x", None)]), RulesSettings())
    assert res.rules == []
    assert res.skipped_accounts == ["x"]
    assert any("no stream rules" in w for w in res.warnings)


# --------------------------------------------------------------------------- keyword rules


def test_keyword_rule_format() -> None:
    kw = KeywordRule("big-account-cashtags", ("TSLA", "NVDA", "AMD"), 1_000_000, "-is:retweet -is:reply lang:en")
    res = build_rules(wl([], [kw]), RulesSettings())
    assert res.rules == [
        RuleSpec(
            value="($TSLA OR $NVDA OR $AMD) min_followers:1000000 -is:retweet -is:reply lang:en",
            tag="ceoscan:kw:big-account-cashtags:1",
        )
    ]


def test_keyword_rule_single_ticker_and_empty_extra_operators() -> None:
    res = build_rules(wl([], [KeywordRule("one", ("TSLA",), 500_000, "  ")]), RulesSettings())
    assert [r.value for r in res.rules] == ["$TSLA min_followers:500000"]


def test_keyword_rule_dedupes_tickers_and_warns_on_low_min_followers() -> None:
    res = build_rules(wl([], [KeywordRule("k", ("TSLA", "$tsla", "NVDA"), 100, "")]), RulesSettings())
    assert [r.value for r in res.rules] == ["($TSLA OR $NVDA) min_followers:100"]
    assert any("min_followers:100" in w for w in res.warnings)


def test_keyword_rule_splits_when_long() -> None:
    tickers = tuple(f"TK{i:03d}" for i in range(250))
    tail = "min_followers:2000000 -is:retweet -is:reply lang:en"
    kw = KeywordRule("big", tickers, 2_000_000, "-is:retweet   -is:reply lang:en")
    res = build_rules(wl([], [kw]), RulesSettings(max_rule_length=200))
    rules = kw_rules(res)
    assert len(rules) > 1
    assert [r.tag for r in rules] == [f"ceoscan:kw:big:{n}" for n in range(1, len(rules) + 1)]
    seen: list[str] = []
    for rule in rules:
        assert len(rule.value) <= 200
        assert rule.value.endswith(" " + tail)
        group = rule.value[: -len(tail) - 1]
        assert group.startswith("(") and group.endswith(")")
        seen.extend(t.lstrip("$") for t in group.strip("()").split(" OR "))
    assert seen == list(tickers)  # every ticker once, in order


def test_keyword_rule_without_tickers_is_skipped() -> None:
    res = build_rules(wl([], [KeywordRule("empty", ())]), RulesSettings())
    assert res.rules == []
    assert any("'empty'" in w and "no tickers" in w for w in res.warnings)


# --------------------------------------------------------------------------- tags, limits, determinism


def test_tags_are_deterministic_and_well_formed() -> None:
    accounts = [acct(f"u{i}", big_id(i), replies=i % 2 == 0) for i in range(120)]
    kws = [KeywordRule("big", tuple(f"Q{i:03d}" for i in range(120)), 1_000_000, "lang:en")]
    settings = RulesSettings(max_rule_length=300, max_rules=10_000)
    first = build_rules(wl(accounts, kws), settings)
    second = build_rules(wl(list(accounts), list(kws)), settings)
    assert first.rules == second.rules
    tags = [r.tag for r in first.rules]
    assert len(tags) == len(set(tags))
    for tag in tags:
        assert TAG_ACCT_RE.match(tag) or TAG_KW_RE.match(tag), tag
    # per-group numbering is 1..n
    for group in ("acct:r1t0", "acct:r0t0", "kw:big"):
        nums = [int(t.rsplit(":", 1)[1]) for t in tags if f":{group}:" in t]
        assert nums == list(range(1, len(nums) + 1))
    # account rules come before keyword rules; ids stay in watchlist order within a group
    kinds = [":kw:" in t for t in tags]
    assert kinds == sorted(kinds)
    r1_ids = [uid for r in first.rules if ":acct:r1t0:" in r.tag for uid in FROM_RE.findall(r.value)]
    assert r1_ids == [a.user_id for a in accounts if a.include_replies]


def test_custom_tag_prefix() -> None:
    res = build_rules(wl([acct("a", "1")], [KeywordRule("k", ("TSLA",))]), RulesSettings(tag_prefix="myapp"))
    assert [r.tag for r in res.rules] == ["myapp:acct:r1t0:1", "myapp:kw:k:1"]


def test_too_many_rules_raises_value_error() -> None:
    accounts = [acct(f"u{i}", big_id(i)) for i in range(50)]
    with pytest.raises(ValueError, match="max|allows"):
        build_rules(wl(accounts), RulesSettings(max_rule_length=60, max_rules=5))


def test_exactly_max_rules_is_allowed() -> None:
    accounts = [acct(f"u{i}", str(i), replies=bool(i % 2), retweets=bool(i // 2 % 2)) for i in range(4)]
    res = build_rules(wl(accounts), RulesSettings(max_rules=4))
    assert len(res.rules) == 4


def test_single_unit_that_cannot_fit_raises_value_error() -> None:
    with pytest.raises(ValueError, match="cannot fit"):
        build_rules(wl([acct("a", big_id(1), replies=False)]), RulesSettings(max_rule_length=20))
    kw = KeywordRule("k", ("TSLA",), 1_000_000, "-is:retweet -is:reply lang:en")
    with pytest.raises(ValueError, match="cannot fit"):
        build_rules(wl([], [kw]), RulesSettings(max_rule_length=30))


def test_real_watchlist_file_builds_valid_rules() -> None:
    watchlist = load_watchlist(ROOT / "config" / "watchlist.yaml")
    with_ids = replace(
        watchlist, accounts=tuple(replace(a, user_id=str(1000 + i)) for i, a in enumerate(watchlist.accounts))
    )
    settings = XApiSettings().rules
    res = build_rules(with_ids, settings)
    assert res.skipped_accounts == []
    assert all(len(r.value) <= settings.max_rule_length for r in res.rules)
    counts = Counter(uid for r in acct_rules(res) for uid in FROM_RE.findall(r.value))
    assert set(counts) == {a.user_id for a in with_ids.accounts} and set(counts.values()) == {1}
    assert len(kw_rules(res)) >= len(with_ids.keyword_rules)
    # the unmodified file has no user_ids yet -> nothing streamed from accounts, all reported
    raw = build_rules(watchlist, settings)
    assert len(raw.skipped_accounts) == sum(1 for a in watchlist.accounts if not a.user_id)


# --------------------------------------------------------------------------- plan_sync


def test_plan_sync_add_delete_unchanged_foreign() -> None:
    desired = [
        RuleSpec("from:1 -is:retweet", "ceoscan:acct:r1t0:1"),
        RuleSpec("$TSLA min_followers:5", "ceoscan:kw:k:1"),
    ]
    existing = [
        {"id": "10", "value": "from:1 -is:retweet", "tag": "ceoscan:acct:r1t0:1"},  # unchanged
        {"id": "11", "value": "from:9 -is:retweet", "tag": "ceoscan:acct:r1t0:2"},  # stale -> delete
        {"id": "12", "value": "cats has:images", "tag": "someone-else"},  # foreign
        {"id": "13", "value": "dogs"},  # foreign (no tag)
        {"id": "14", "value": "birds", "tag": "ceoscanner:x"},  # prefix without ":" boundary -> foreign
    ]
    plan = plan_sync(desired, existing, "ceoscan", manage_foreign=False)
    assert isinstance(plan, RuleSyncPlan)
    assert plan.to_add == [desired[1]]
    assert [r["id"] for r in plan.unchanged] == ["10"]
    assert [r["id"] for r in plan.to_delete] == ["11"]
    assert [r["id"] for r in plan.foreign] == ["12", "13", "14"]

    managed = plan_sync(desired, existing, "ceoscan", manage_foreign=True)
    assert [r["id"] for r in managed.to_delete] == ["11", "12", "13", "14"]
    assert [r["id"] for r in managed.foreign] == ["12", "13", "14"]


def test_plan_sync_matches_by_exact_value_not_tag() -> None:
    desired = [RuleSpec("from:1 -is:retweet", "ceoscan:acct:r1t0:1")]
    existing = [
        {"id": "1", "value": "from:1 -is:retweet", "tag": "ceoscan:acct:r1t0:7"},  # same value, other tag
        {"id": "2", "value": "from:1  -is:retweet", "tag": "ceoscan:acct:r1t0:1"},  # same tag, other value
    ]
    plan = plan_sync(desired, existing, "ceoscan", manage_foreign=False)
    assert plan.to_add == []
    assert [r["id"] for r in plan.unchanged] == ["1"]
    assert [r["id"] for r in plan.to_delete] == ["2"]


def test_plan_sync_empty_existing_and_empty_desired() -> None:
    desired = [RuleSpec("from:1", "ceoscan:acct:r1t1:1"), RuleSpec("from:1", "ceoscan:acct:r1t1:2")]
    plan = plan_sync(desired, [], "ceoscan", manage_foreign=False)
    assert plan.to_add == [desired[0]]  # duplicate values are added once
    assert plan.to_delete == plan.unchanged == plan.foreign == []

    existing = [{"id": "1", "value": "from:1", "tag": "ceoscan:acct:r1t1:1"}, {"id": "2", "value": "x", "tag": "y"}]
    plan = plan_sync([], existing, "ceoscan", manage_foreign=False)
    assert [r["id"] for r in plan.to_delete] == ["1"]
    assert [r["id"] for r in plan.foreign] == ["2"]


def test_plan_sync_foreign_rule_with_desired_value_is_reused() -> None:
    desired = [RuleSpec("from:1", "ceoscan:acct:r1t1:1")]
    existing = [{"id": "5", "value": "from:1", "tag": "manual"}]
    for manage in (False, True):
        plan = plan_sync(desired, existing, "ceoscan", manage_foreign=manage)
        assert plan.to_add == [] and plan.to_delete == [] and plan.foreign == []
        assert [r["id"] for r in plan.unchanged] == ["5"]


# --------------------------------------------------------------------------- sync_rules (mocked X)


class Recorder:
    """aioresponses callback that records every rules-endpoint call in order.

    ``dry_run_response`` (if given) is returned for dry-run add requests instead of "all valid".
    """

    def __init__(self, existing: list[dict], dry_run_response: dict | None = None) -> None:
        self.existing = existing
        self.dry_run_response = dry_run_response
        self.calls: list[tuple[str, bool, dict | None]] = []

    def __call__(self, url, method: str, **kwargs) -> CallbackResult:
        body = kwargs.get("json")
        dry = url.query.get("dry_run") == "true"
        self.calls.append((method, dry, body))
        if method == "GET":
            return CallbackResult(
                status=200, payload={"data": self.existing, "meta": {"result_count": len(self.existing)}}
            )
        if "delete" in body:
            n = len(body["delete"]["ids"])
            return CallbackResult(status=200, payload={"meta": {"summary": {"deleted": n, "not_deleted": 0}}})
        adds = body["add"]
        if dry and self.dry_run_response is not None:
            return CallbackResult(status=200, payload=self.dry_run_response)
        data = [{"id": str(900 + i), **a} for i, a in enumerate(adds)]
        summary = {"created": len(adds), "not_created": 0, "valid": len(adds), "invalid": 0}
        return CallbackResult(status=201, payload={"data": data, "meta": {"summary": summary}})


def _mock_rules(m: aioresponses, rec: Recorder) -> None:
    m.get(RULES_URL, callback=lambda url, **kw: rec(url, "GET", **kw), repeat=True)
    m.post(RULES_URL, callback=lambda url, **kw: rec(url, "POST", **kw), repeat=True)


DESIRED = [
    RuleSpec("(from:1 OR from:2) -is:retweet", "ceoscan:acct:r1t0:1"),
    RuleSpec("$TSLA min_followers:5", "ceoscan:kw:k:1"),
]
EXISTING = [
    {"id": "100", "value": "(from:1 OR from:2) -is:retweet", "tag": "ceoscan:acct:r1t0:1"},
    {"id": "101", "value": "from:3 -is:retweet", "tag": "ceoscan:acct:r1t0:2"},
    {"id": "102", "value": "cats", "tag": "other-app"},
]


async def test_sync_rules_validates_with_dry_run_then_deletes_then_adds() -> None:
    rec = Recorder(EXISTING)
    async with aiohttp.ClientSession() as session:
        client = XClient(session, "test-bearer-token", XApiSettings())
        with aioresponses() as m:
            _mock_rules(m, rec)
            plan = await sync_rules(client, DESIRED, RulesSettings(), apply=True)
    assert plan.to_add == [DESIRED[1]]
    assert [r["id"] for r in plan.to_delete] == ["101"]
    assert [r["id"] for r in plan.foreign] == ["102"]
    assert [(m_, dry) for m_, dry, _ in rec.calls] == [("GET", False), ("POST", True), ("POST", False), ("POST", False)]
    assert rec.calls[1][2] == {"add": [{"value": "$TSLA min_followers:5", "tag": "ceoscan:kw:k:1"}]}
    assert rec.calls[2][2] == {"delete": {"ids": ["101"]}}
    assert rec.calls[3][2] == {"add": [{"value": "$TSLA min_followers:5", "tag": "ceoscan:kw:k:1"}]}


async def test_sync_rules_manage_foreign_deletes_foreign() -> None:
    rec = Recorder(EXISTING)
    async with aiohttp.ClientSession() as session:
        client = XClient(session, "test-bearer-token", XApiSettings())
        with aioresponses() as m:
            _mock_rules(m, rec)
            await sync_rules(client, DESIRED, RulesSettings(manage_foreign_rules=True))
    assert rec.calls[2][2] == {"delete": {"ids": ["101", "102"]}}


async def test_sync_rules_raises_on_invalid_rule_before_changing_anything() -> None:
    errors = [
        {
            "value": "$TSLA min_followers:5",
            "details": ["Reference to invalid operator 'min_followers'"],
            "title": "UnprocessableEntity",
            "type": "https://api.twitter.com/2/problems/invalid-rules",
        }
    ]
    summary = {"created": 0, "not_created": 1, "valid": 0, "invalid": 1}
    rec = Recorder(EXISTING, dry_run_response={"errors": errors, "meta": {"summary": summary}})
    async with aiohttp.ClientSession() as session:
        client = XClient(session, "test-bearer-token", XApiSettings())
        with aioresponses() as m:
            _mock_rules(m, rec)
            with pytest.raises(ValueError) as exc_info:
                await sync_rules(client, DESIRED, RulesSettings(), apply=True)
    msg = str(exc_info.value)
    assert "UnprocessableEntity" in msg and "invalid operator" in msg and "$TSLA min_followers:5" in msg
    assert "test-bearer-token" not in msg
    # only the listing and the dry run happened: nothing deleted, nothing added
    assert [(m_, dry) for m_, dry, _ in rec.calls] == [("GET", False), ("POST", True)]


async def test_sync_rules_dry_run_duplicate_is_not_fatal() -> None:
    dup = {
        "value": "$TSLA min_followers:5",
        "id": "77",
        "title": "DuplicateRule",
        "type": "https://api.twitter.com/2/problems/duplicate-rules",
    }
    summary = {"created": 0, "not_created": 1, "valid": 1, "invalid": 0}
    rec = Recorder(EXISTING, dry_run_response={"errors": [dup], "meta": {"summary": summary}})
    async with aiohttp.ClientSession() as session:
        client = XClient(session, "test-bearer-token", XApiSettings())
        with aioresponses() as m:
            _mock_rules(m, rec)
            plan = await sync_rules(client, DESIRED, RulesSettings(), apply=False)
    assert plan.to_add == [DESIRED[1]]
    assert [(m_, dry) for m_, dry, _ in rec.calls] == [("GET", False), ("POST", True)]


async def test_sync_rules_invalid_count_without_error_details_still_raises() -> None:
    summary = {"created": 0, "not_created": 1, "valid": 0, "invalid": 1}
    rec = Recorder(EXISTING, dry_run_response={"meta": {"summary": summary}})
    async with aiohttp.ClientSession() as session:
        client = XClient(session, "test-bearer-token", XApiSettings())
        with aioresponses() as m:
            _mock_rules(m, rec)
            with pytest.raises(ValueError, match="1 invalid"):
                await sync_rules(client, DESIRED, RulesSettings())


async def test_sync_rules_apply_false_only_validates() -> None:
    rec = Recorder(EXISTING)
    async with aiohttp.ClientSession() as session:
        client = XClient(session, "test-bearer-token", XApiSettings())
        with aioresponses() as m:
            _mock_rules(m, rec)
            plan = await sync_rules(client, DESIRED, RulesSettings(), apply=False)
    assert plan.to_add == [DESIRED[1]] and [r["id"] for r in plan.to_delete] == ["101"]
    assert [(m_, dry) for m_, dry, _ in rec.calls] == [("GET", False), ("POST", True)]


async def test_sync_rules_in_sync_makes_no_changes() -> None:
    existing = [{"id": str(i), "value": r.value, "tag": r.tag} for i, r in enumerate(DESIRED)]
    rec = Recorder(existing)
    async with aiohttp.ClientSession() as session:
        client = XClient(session, "test-bearer-token", XApiSettings())
        with aioresponses() as m:
            _mock_rules(m, rec)
            plan = await sync_rules(client, DESIRED, RulesSettings())
    assert plan.to_add == [] and plan.to_delete == [] and len(plan.unchanged) == 2
    assert [(m_, dry) for m_, dry, _ in rec.calls] == [("GET", False)]


async def test_sync_rules_propagates_http_errors() -> None:
    async with aiohttp.ClientSession() as session:
        client = XClient(session, "test-bearer-token", XApiSettings())
        with aioresponses() as m:
            m.get(RULES_URL, status=401, payload={"title": "Unauthorized", "detail": "Unauthorized", "status": 401})
            with pytest.raises(NotAuthorized):
                await sync_rules(client, DESIRED, RulesSettings())


def test_render_term_cashtags_words_and_phrases():
    from ceoscan.xapi.rules import render_term

    assert render_term("$apmd") == "$APMD"
    assert render_term("Apnimed") == "Apnimed"
    assert render_term("AD109") == "AD109"
    assert render_term("AD-109") == '"AD-109"'
    assert render_term("  sleep   apnea pill ") == '"sleep apnea pill"'
    assert render_term("") == ""
