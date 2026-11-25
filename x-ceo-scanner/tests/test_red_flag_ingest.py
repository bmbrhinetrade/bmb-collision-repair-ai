"""Red flags, ingest side: watchlist schema (sources, red-flag term groups, red_flag / source keyword
rules), stream rule rendering and packing, `verify` for sources, the cost estimate, and the read-only
`counts` command (GET /2/tweets/counts/recent). Nothing here touches the real X API.
"""

from __future__ import annotations

import re
import shutil
from collections import Counter
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import aiohttp
import aioresponses.core as aioresponses_core
import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer
from aioresponses import aioresponses

import ceoscan.__main__ as cli
from ceoscan.config import (
    AppConfig,
    ConfigError,
    RulesSettings,
    Secrets,
    Settings,
    XApiSettings,
    load_settings,
    load_watchlist,
)
from ceoscan.models import KeywordRule, Source, Watchlist
from ceoscan.ops.cost import estimate_monthly_cost, format_cost_estimate
from ceoscan.timeutil import datetime_to_snowflake
from ceoscan.xapi.client import COUNTS_QUERY_MAX, CreditsDepleted, XApiError, XClient
from ceoscan.xapi.rules import (
    LOW_MIN_FOLLOWERS_WARNING,
    build_count_queries,
    build_rules,
    keyword_rule_values,
    remove_operator,
    strip_counts_operators,
)
from ceoscan.xapi.verify import (
    VerifyResult,
    assess_source,
    format_verify_report,
    verify_sources,
    write_user_ids,
)

ROOT = Path(__file__).resolve().parents[1]
WATCHLIST = ROOT / "config" / "watchlist.yaml"
SETTINGS = ROOT / "config" / "settings.yaml"
NOW = datetime(2026, 11, 25, 12, 0, tzinfo=UTC)
COUNTS_URL = re.compile(r"^https://api\.x\.com/2/tweets/counts/recent(\?.*)?$")
FROM_RE = re.compile(r"from:(\d+)")

# The five stream rules the user's server runs today (ceoscan:acct:r1t0:1, acct:r0t0:1 and three keyword
# rules): adding red flags must not change a byte of them, or the next start would re-create them.
LIVE_KEYWORD_RULES = {
    "ceoscan:kw:apmd-catalysts:1": (
        "($APMD OR Apnimed OR Oxnimbi OR AD109 OR aroxybutynin OR LunAIRo OR SynAIRgy) min_followers:1000 "
        "-is:retweet -is:reply lang:en"
    ),
    "ceoscan:kw:tln-news:1": '($TLN OR "Talen Energy") min_followers:20000 -is:retweet -is:reply lang:en',
}


class _CompatClientResponse(aiohttp.ClientResponse):
    """aioresponses 0.7.9 does not pass ``stream_writer`` (required by the pinned aiohttp 3.14)."""

    def __init__(self, method: str, url: Any, **kwargs: Any) -> None:
        kwargs.setdefault("stream_writer", SimpleNamespace(output_size=0))
        super().__init__(method, url, **kwargs)


@pytest.fixture(autouse=True)
def _aioresponses_compat(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(aioresponses_core, "ClientResponse", _CompatClientResponse)


def wl_file(tmp_path: Path, text: str) -> Path:
    p = tmp_path / "watchlist.yaml"
    p.write_text(text, encoding="utf-8")
    return p


BASE = """\
my_tickers: [TSLA, NVDA]
accounts:
  - handle: elonmusk
    user_id: "44196397"
    tickers: [TSLA]
sources:
  - handle: SECGov
    kind: regulator
    expect_name: U.S. Securities and Exchange Commission
    trusted: true
    user_id: "18481018"
    est_posts_per_day: 0.5
  - handle: HindenburgRes
    kind: short_seller
    est_posts_per_day: 1.5
  - handle: DeItaone
    kind: newswire
    user_id: "2704294333"
    est_posts_per_day: 4
red_flag_terms: [fraud, lawsuit, "short report", recall]
red_flag_names: [Tesla, Nvidia, "Eli Lilly"]
keyword_rules:
"""

RED_FLAG_RULE = """\
  - name: red-flags
    kind: red_flag
    tickers: all
    terms: "@red_flag_names"
    require_any: "@red_flag_terms"
    min_followers: 10000
    extra_operators: "-is:retweet -is:reply lang:en"
    est_posts_per_day: 40
"""

SOURCE_RULE = """\
  - name: red-flag-sources
    kind: source
    from: sources
    tickers: all
    terms: "@red_flag_names"
    extra_operators: "-is:retweet"
"""


# --------------------------------------------------------------------------- watchlist schema


def test_sources_term_groups_and_red_flag_rules_load(tmp_path: Path) -> None:
    wl = load_watchlist(wl_file(tmp_path, BASE + RED_FLAG_RULE + SOURCE_RULE))
    assert [s.handle for s in wl.sources] == ["SECGov", "HindenburgRes", "DeItaone"]
    sec = wl.sources[0]
    assert sec == Source(
        "SECGov", "regulator", "U.S. Securities and Exchange Commission", "18481018", True, 0.5, ""
    )
    assert wl.sources[1].user_id is None and wl.sources[1].trusted is False and wl.sources[1].kind == "short_seller"
    assert set(wl.sources_by_user_id()) == {"18481018", "2704294333"}

    red, src = wl.keyword_rules
    assert red.kind == "red_flag" and red.tickers == ("TSLA", "NVDA")
    assert red.terms == ("Tesla", "Nvidia", "Eli Lilly")
    assert red.require_any == ("fraud", "lawsuit", "short report", "recall")
    assert red.from_ids == () and red.min_followers == 10_000

    assert src.kind == "source" and src.terms == red.terms and src.require_any == ()
    assert src.from_ids == ("18481018", "2704294333")  # the unverified source is skipped
    assert src.min_followers == 0  # default for source rules: the authors already limit it
    assert src.extra_operators == "-is:retweet"
    assert src.est_posts_per_day == pytest.approx(0.5 + 1.5 + 4)  # default: the sources' sum (all of them)


def test_term_groups_mix_with_words_and_from_handle_list(tmp_path: Path) -> None:
    text = BASE + (
        "  - name: shorts\n    kind: source\n    from: [hindenburgres, '@DeItaone']\n    tickers: [TSLA]\n"
        '    terms: ["@red_flag_names", Cybertruck]\n    require_any: ["@red_flag_terms", "going concern"]\n'
        "    min_followers: 500\n    est_posts_per_day: 3\n"
    )
    rule = load_watchlist(wl_file(tmp_path, text)).keyword_rules[0]
    assert rule.terms == ("Tesla", "Nvidia", "Eli Lilly", "Cybertruck")
    assert rule.require_any == ("fraud", "lawsuit", "short report", "recall", "going concern")
    assert rule.from_ids == ("2704294333",) and rule.min_followers == 500 and rule.est_posts_per_day == 3


@pytest.mark.parametrize(
    ("snippet", "needle"),
    [
        ("  - name: x\n    kind: redflag\n    require_any: [fraud]\n", "kind must be keyword, red_flag or source"),
        ("  - name: x\n    kind: red_flag\n    tickers: [TSLA]\n", "kind: red_flag needs require_any"),
        ("  - name: x\n    kind: source\n    tickers: [TSLA]\n", "kind: source needs from: sources"),
        ("  - name: x\n    from: sources\n    tickers: [TSLA]\n", "from: is only used by kind: source rules"),
        ("  - name: x\n    kind: source\n    from: [nobody]\n", "'nobody' is not a handle in the top-level sources"),
        ("  - name: x\n    kind: source\n    from: everyone\n", ".from must be sources"),
        ("  - name: x\n    terms: '@red_flag_word'\n", "unknown term group '@red_flag_word'"),
        ("  - name: x\n    require_any: fraud\n", ".require_any must be a list"),
        ("  - name: x\n    require_any: ['(fraud)']\n", "is not a valid term"),
        ("  - name: x\n    requires_any: [fraud]\n", "requires_any (did you mean require_any?)"),
        ("  - name: x\n    form: sources\n", "form (did you mean from?)"),
        ("  - name: 'a:b'\n    tickers: [TSLA]\n", "name must be 1-64 printable characters without ':'"),
        (
            "  - name: x\n    tickers: [TSLA]\n  - name: x\n    tickers: [NVDA]\n",
            "duplicate keyword rule name(s): x",
        ),
    ],
)
def test_invalid_red_flag_rules_are_rejected(tmp_path: Path, snippet: str, needle: str) -> None:
    with pytest.raises(ConfigError) as info:
        load_watchlist(wl_file(tmp_path, BASE + snippet))
    assert needle in str(info.value)


@pytest.mark.parametrize(
    ("text", "needle"),
    [
        ("sources:\n  - handle: SECGov\n    kind: agency\n", "sources[0] (@SECGov).kind must be one of short_seller"),
        ("sources:\n  - handle: SECGov\n    trust: true\n", "trust (did you mean trusted?)"),
        ("sources:\n  - handle: SECGov\n    trusted: 'yes'\n", "trusted must be true or false"),
        ("sources:\n  - handle: SECGov\n    user_id: abc\n", "user_id must be numeric"),
        ("sources:\n  - handle: 'not a handle'\n", "invalid or missing handle"),
        ("sources:\n  - handle: a\n  - handle: A\n", "duplicate source handle"),
        ("sources: SECGov\n", "sources must be a list"),
        (
            "accounts:\n  - handle: elonmusk\nsources:\n  - handle: ElonMusk\n",
            "@ElonMusk is also in accounts",
        ),
        ("red_flag_terms: fraud\n", "red_flag_terms must be a list"),
        ("red_flag_terms: ['fraud:x']\n", "red_flag_terms: 'fraud:x' is not a valid term"),
        ("red_flag_term: [fraud]\n", "red_flag_term (did you mean red_flag_terms?)"),
        (
            "my_tickers: [TSLA]\nkeyword_rules:\n  - name: r\n    kind: red_flag\n    require_any: '@red_flag_terms'\n",
            "uses @red_flag_terms, but the watchlist has no (or an empty) top-level red_flag_terms",
        ),
    ],
)
def test_invalid_sources_and_term_groups_are_rejected(tmp_path: Path, text: str, needle: str) -> None:
    with pytest.raises(ConfigError) as info:
        load_watchlist(wl_file(tmp_path, text))
    assert needle in str(info.value)


def test_shipped_watchlist_has_the_red_flag_rules() -> None:
    wl = load_watchlist(WATCHLIST)
    rules = {k.name: k for k in wl.keyword_rules}
    mega, red, src = rules["red-flags-mega"], rules["red-flags"], rules["red-flag-sources"]
    # two tiers by chatter level: together they cover every ticker exactly once
    assert mega.kind == red.kind == "red_flag"
    assert mega.min_followers == 100_000 and red.min_followers == 10_000
    assert not set(mega.tickers) & set(red.tickers)
    assert sorted(mega.tickers + red.tickers) == sorted(wl.my_tickers)
    assert {"TSLA", "NVDA", "AAPL"} <= set(mega.tickers) and {"APMD", "LLY", "CME"} <= set(red.tickers)
    for tier in (mega, red):
        assert tier.extra_operators == "-is:retweet -is:reply lang:en"
        assert tier.require_any == red.require_any  # the shared red_flag_terms group
        assert "Visa" not in tier.terms  # an everyday word: Visa is covered by $V only
    assert {"Tesla", "Nvidia", "Apple"} <= set(mega.terms)
    assert {"Eli Lilly", "CME Group", "Constellation Energy", "Apnimed"} <= set(red.terms)
    # the source rule names every company; each tier's names belong to its own tickers
    assert set(mega.terms) | set(red.terms) == set(src.terms) and not set(mega.terms) & set(red.terms)
    assert {"short report", "fraud", "going concern", "Wells notice", "clinical hold"} <= set(red.require_any)
    assert src.kind == "source" and src.min_followers == 0 and src.extra_operators == "-is:retweet"
    assert src.require_any == ()
    # wires are left out of the source rule (the red-flag rules catch their posts), but labelled as sources
    by_handle = {s.handle: s for s in wl.sources}
    assert all(s.user_id for s in wl.sources)
    assert len(src.from_ids) == len(set(src.from_ids)) >= 10
    assert set(src.from_ids) <= {s.user_id for s in wl.sources}
    assert by_handle["Reuters"].user_id not in src.from_ids and by_handle["SECGov"].user_id in src.from_ids
    trusted = {s.handle for s in wl.sources if s.trusted}
    assert {"SECGov", "FDA", "Reuters"} <= trusted
    assert not trusted & {s.handle for s in wl.sources if s.kind == "short_seller"}
    assert "DeItaone" not in trusted and "unusual_whales" not in trusted


def test_existing_watchlists_still_load_unchanged(tmp_path: Path) -> None:
    text = "my_tickers: [TSLA]\nkeyword_rules:\n  - name: big\n    tickers: all\n    min_followers: 0\n"
    rule = load_watchlist(wl_file(tmp_path, text)).keyword_rules[0]
    assert rule == KeywordRule("big", ("TSLA",), 0, "-is:retweet -is:reply", 50.0)
    assert load_watchlist(wl_file(tmp_path, "")).sources == ()


# --------------------------------------------------------------------------- settings


def test_shipped_settings_red_flags_match_the_defaults() -> None:
    s = load_settings(SETTINGS)
    assert s.red_flags == Settings().red_flags
    assert s.x_api.pricing.counts_request == 0.005


@pytest.mark.parametrize(
    ("yaml_text", "needle"),
    [
        ("red_flags:\n  min_score: 0\n", "red_flags.min_score must be 1-10"),
        ("red_flags:\n  spread_min_authors: 1\n", "red_flags.spread_min_authors must be 2-100"),
        ("red_flags:\n  spread_window_minutes: 0\n", "red_flags.spread_window_minutes must be 1-1440"),
        ("red_flags:\n  spread_cooldown_minutes: -5\n", "red_flags.spread_cooldown_minutes must be 0-10080"),
        ("red_flags:\n  denial_lookback_hours: 0\n", "red_flags.denial_lookback_hours must be 1-720"),
        ("red_flags:\n  suspicious_account_age_days: -1\n", "red_flags.suspicious_account_age_days"),
        ("red_flags:\n  established_min_followers: -1\n", "red_flags.established_min_followers must be >= 0"),
        (
            "red_flags:\n  established_min_followers: 2000000\n",
            "red_flags.trusted_min_followers (1,000,000) must be >= red_flags.established_min_followers (2,000,000)",
        ),
        ("red_flags:\n  trusted_domains: [https://sec.gov]\n", "red_flags.trusted_domains[0] must be a bare domain"),
        ("red_flags:\n  trusted_domains: [sec.gov, '*.reuters.com']\n", "red_flags.trusted_domains[1]"),
        ("red_flags:\n  trusted_domains: [localhost]\n", "red_flags.trusted_domains[0]"),
        ("red_flags:\n  trusted_domains: sec.gov\n", "settings.red_flags.trusted_domains must be a list"),
        ("red_flags:\n  alert_unverifed: true\n", "alert_unverifed (did you mean alert_unverified?)"),
        ("x_api:\n  pricing:\n    counts_request: -1\n", "x_api.pricing.counts_request must be >= 0"),
    ],
)
def test_invalid_red_flag_settings(tmp_path: Path, yaml_text: str, needle: str) -> None:
    p = tmp_path / "settings.yaml"
    p.write_text(yaml_text, encoding="utf-8")
    with pytest.raises(ConfigError) as info:
        load_settings(p)
    assert needle in str(info.value)


def test_valid_domains_are_accepted(tmp_path: Path) -> None:
    p = tmp_path / "settings.yaml"
    p.write_text("red_flags:\n  trusted_domains: [news.bbc.co.uk, SEC.gov, x-y.io]\n", encoding="utf-8")
    assert load_settings(p).red_flags.trusted_domains == ["news.bbc.co.uk", "SEC.gov", "x-y.io"]


# --------------------------------------------------------------------------- stream rules


def test_red_flag_and_source_rules_render(tmp_path: Path) -> None:
    wl = load_watchlist(wl_file(tmp_path, BASE + RED_FLAG_RULE + SOURCE_RULE))
    res = build_rules(wl, RulesSettings())
    by_tag = {r.tag: r.value for r in res.rules}
    assert by_tag["ceoscan:kw:red-flags:1"] == (
        '($TSLA OR $NVDA OR Tesla OR Nvidia OR "Eli Lilly") (fraud OR lawsuit OR "short report" OR recall) '
        "min_followers:10000 -is:retweet -is:reply lang:en"
    )
    assert by_tag["ceoscan:kw:red-flag-sources:1"] == (
        '(from:18481018 OR from:2704294333) ($TSLA OR $NVDA OR Tesla OR Nvidia OR "Eli Lilly") -is:retweet'
    )
    assert res.skipped_sources == ["HindenburgRes"]
    assert any("1 source(s) have no verified user_id" in w and "@HindenburgRes" in w for w in res.warnings)
    assert not any("min_followers" in w for w in res.warnings)  # 10K red flags, source rules: no warning


def test_source_rule_without_verified_sources_is_skipped_never_unrestricted(tmp_path: Path) -> None:
    text = BASE.replace('    user_id: "18481018"\n', "").replace('    user_id: "2704294333"\n', "") + SOURCE_RULE
    wl = load_watchlist(wl_file(tmp_path, text))
    assert wl.keyword_rules[0].from_ids == ()
    res = build_rules(wl, RulesSettings())
    assert not any(":kw:" in r.tag for r in res.rules)  # never "(... names ...) -is:retweet" from anyone
    assert any("'red-flag-sources' (kind source) has no source with a verified user_id" in w for w in res.warnings)
    assert res.skipped_sources == ["SECGov", "HindenburgRes", "DeItaone"]
    assert "Sources skipped - no verified user_id, NOT streamed (3)" in cli.format_rule_build(
        res, RulesSettings(), Path("w.yaml")
    )


def test_min_followers_zero_is_omitted_and_low_red_flag_threshold_warns() -> None:
    kw = KeywordRule("rf", ("TSLA",), 0, "lang:en", terms=("Tesla",), require_any=("fraud",), kind="red_flag")
    assert keyword_rule_values(kw, 1024) == ["($TSLA OR Tesla) fraud lang:en"]
    wl = Watchlist((), (kw,), ("TSLA",), {})
    res = build_rules(wl, RulesSettings())
    assert any(w.startswith("red-flag rule 'rf' uses min_followers:0") for w in res.warnings)
    ok = replace(kw, min_followers=LOW_MIN_FOLLOWERS_WARNING)
    assert not any("min_followers" in w for w in build_rules(Watchlist((), (ok,), ("TSLA",), {}), RulesSettings()).warnings)


def _ids(n: int) -> tuple[str, ...]:
    return tuple(str(1_400_000_000_000_000_000 + i * 104_729) for i in range(n))


@pytest.mark.parametrize(("n_ids", "n_tickers", "max_len"), [(1, 3, 1024), (40, 90, 1024), (25, 60, 512), (7, 200, 300)])
def test_two_dimensional_packing_covers_every_pair_once(n_ids: int, n_tickers: int, max_len: int) -> None:
    tickers = tuple(f"T{i:03d}" for i in range(n_tickers))
    kw = KeywordRule(
        "src", tickers, 0, "-is:retweet", terms=("Some Company",), require_any=("fraud", "probe"),
        from_ids=_ids(n_ids), kind="source",
    )
    values = keyword_rule_values(kw, max_len)
    assert all(len(v) <= max_len for v in values)
    pairs: Counter[tuple[str, str]] = Counter()
    for v in values:
        assert v.endswith(" (fraud OR probe) -is:retweet")
        ids = FROM_RE.findall(v)
        items = re.findall(r"\$T\d{3}|\"Some Company\"", v)
        pairs.update((i, t) for i in ids for t in items)
    expected = {(i, t) for i in _ids(n_ids) for t in [*(f"${t}" for t in tickers), '"Some Company"']}
    assert set(pairs) == expected and set(pairs.values()) == {1}
    # never more values than one-id-per-rule with the same term packing would need
    assert len(values) <= n_ids * len(keyword_rule_values(replace(kw, from_ids=(_ids(1)[0],)), max_len))


def test_packing_raises_when_one_id_and_one_term_cannot_fit() -> None:
    kw = KeywordRule("src", ("TSLA",), 0, "", require_any=("x" * 40,), from_ids=_ids(1), kind="source")
    with pytest.raises(ValueError, match="cannot fit"):
        keyword_rule_values(kw, 60)


def test_max_rules_is_respected_for_packed_red_flag_rules() -> None:
    kw = KeywordRule("src", tuple(f"T{i:03d}" for i in range(200)), 0, "", from_ids=_ids(50), kind="source")
    with pytest.raises(ValueError, match="plan allows 3"):
        build_rules(Watchlist((), (kw,), (), {}), RulesSettings(max_rule_length=300, max_rules=3))


def test_shipped_rules_keep_the_live_rules_byte_identical() -> None:
    """The red-flag rules are added after the live ones; the five rules on X stay exactly as they are."""
    wl = load_watchlist(WATCHLIST)
    full = build_rules(wl, XApiSettings().rules)
    old = [k for k in wl.keyword_rules if k.kind == "keyword"]
    before = build_rules(replace(wl, keyword_rules=tuple(old)), XApiSettings().rules)
    assert len(before.rules) == 5
    assert full.rules[:5] == before.rules
    values = {r.tag: r.value for r in full.rules}
    for tag, value in LIVE_KEYWORD_RULES.items():
        assert values[tag] == value
    for name, followers in (("red-flags-mega", 100_000), ("red-flags", 10_000)):
        red = [r for r in full.rules if r.tag.startswith(f"ceoscan:kw:{name}:")]
        assert red and [r.tag for r in red] == [f"ceoscan:kw:{name}:{n}" for n in range(1, len(red) + 1)]
        assert all(len(r.value) <= 1024 for r in red)
        assert all(r.value.endswith(f" min_followers:{followers} -is:retweet -is:reply lang:en") for r in red)
    sources = [r for r in full.rules if ":kw:red-flag-sources:" in r.tag]
    assert sources and all(r.value.startswith("(from:") and len(r.value) <= 1024 for r in sources)
    assert all("min_followers" not in r.value and r.value.endswith(" -is:retweet") for r in sources)


# --------------------------------------------------------------------------- counts queries


def test_strip_and_remove_operators() -> None:
    assert strip_counts_operators("-is:retweet sample:10 lang:en followers_count:10..20") == (
        "-is:retweet lang:en",
        ["sample:10", "followers_count:10..20"],
    )
    assert strip_counts_operators("") == ("", [])
    assert remove_operator('($TSLA OR "a b") has:links -has:links lang:en', "has") == '($TSLA OR "a b") lang:en'


def test_count_queries_drop_min_followers_and_fit_512_chars() -> None:
    wl = load_watchlist(WATCHLIST)
    queries, notes = build_count_queries(wl, XApiSettings().rules)
    assert queries and all(len(q.query) <= COUNTS_QUERY_MAX for q in queries)
    assert not any("min_followers" in q.query for q in queries)
    assert not any("red-flag-sources: skipped" in n for n in notes)  # the shipped sources are verified
    by_rule: dict[str, list[Any]] = {}
    for q in queries:
        by_rule.setdefault(q.rule, []).append(q)
    assert set(by_rule) == {
        "big-account-cashtags", "apmd-catalysts", "tln-news", "red-flags-mega", "red-flags", "red-flag-sources",
    }
    assert all(q.query.startswith("(from:") for q in by_rule["red-flag-sources"])
    assert by_rule["tln-news"][0].query == '($TLN OR "Talen Energy") -is:retweet -is:reply lang:en'
    assert by_rule["tln-news"][0].removed == ("min_followers:20000",)
    red = by_rule["red-flags"]
    assert [q.n for q in red] == list(range(1, len(red) + 1)) and all(q.kind == "red_flag" for q in red)
    # the 600+ character red-flag group cannot fit in 512: it is split, and every word is still asked
    words = {w for w in {k.name: k for k in wl.keyword_rules}["red-flags"].require_any}
    assert words
    for name in ("red-flags-mega", "red-flags"):
        rendered = " ".join(q.query for q in by_rule[name])
        assert all((f'"{w}"' if " " in w else w) in rendered for w in words)


def test_count_queries_for_source_rules_keep_the_from_group(tmp_path: Path) -> None:
    wl = load_watchlist(wl_file(tmp_path, BASE + SOURCE_RULE))
    (q,), _ = build_count_queries(wl, RulesSettings())
    assert q.query.startswith("(from:18481018 OR from:2704294333) ") and q.removed == ()


# --------------------------------------------------------------------------- XClient.count_recent


COUNTS_BODY = {
    "data": [
        {"start": "2026-11-18T12:00:00.000Z", "end": "2026-11-19T00:00:00.000Z", "tweet_count": 10},
        {"start": "2026-11-19T00:00:00.000Z", "end": "2026-11-25T12:00:00.000Z", "tweet_count": 60},
    ],
    "meta": {"total_tweet_count": 70},
}


async def test_count_recent_sends_query_and_parses() -> None:
    async with aiohttp.ClientSession() as session:
        client = XClient(session, "test-bearer-token", XApiSettings())
        with aioresponses() as m:
            m.get(COUNTS_URL, payload=COUNTS_BODY)
            body = await client.count_recent("  $TSLA   fraud ", start_time=datetime(2026, 11, 20, 1, 2, 3, tzinfo=UTC))
            ((_, url), calls), = m.requests.items()
    assert body["meta"]["total_tweet_count"] == 70
    assert url.query["query"] == "$TSLA fraud" and url.query["granularity"] == "day"
    assert url.query["start_time"] == "2026-11-20T01:02:03Z"
    assert calls[0].kwargs["headers"]["Authorization"] == "Bearer test-bearer-token"


async def test_count_recent_errors() -> None:
    async with aiohttp.ClientSession() as session:
        client = XClient(session, "test-bearer-token", XApiSettings())
        with pytest.raises(ValueError, match="at most 512"):
            await client.count_recent("x" * 513)
        with pytest.raises(ValueError, match="empty"):
            await client.count_recent("  ")
        with aioresponses() as m:
            m.get(COUNTS_URL, status=402, payload={"title": "CreditsDepleted", "detail": "no credits"})
            with pytest.raises(CreditsDepleted):
                await client.count_recent("$TSLA")
            m.get(COUNTS_URL, status=400, payload={"title": "Invalid Request", "detail": "bad"})
            with pytest.raises(XApiError) as info:
                await client.count_recent("$TSLA")
    assert info.value.status == 400 and "test-bearer-token" not in str(info.value)


# --------------------------------------------------------------------------- `counts` command


class FakeCounts:
    def __init__(self) -> None:
        self.queries: list[dict[str, str]] = []
        self.status = 200
        self.reject_operator: str | None = None

    async def handle(self, request: web.Request) -> web.StreamResponse:
        q = dict(request.query)
        self.queries.append(q)
        if self.status == 402:
            return web.json_response({"title": "CreditsDepleted", "detail": "no credits"}, status=402)
        if self.reject_operator and f"{self.reject_operator}:" in q["query"]:
            msg = f"There were errors processing your request: Unrecognized operator:'{self.reject_operator}'"
            return web.json_response(
                {"title": "Invalid Request", "detail": "One or more parameters to your request was invalid.",
                 "errors": [{"message": msg, "parameters": {"query": [q["query"]]}}]},
                status=400,
            )
        return web.json_response(COUNTS_BODY)


@pytest.fixture
async def fake_counts():
    srv = FakeCounts()
    app = web.Application()
    app.router.add_get("/2/tweets/counts/recent", srv.handle)
    server = TestServer(app, host="127.0.0.1")
    await server.start_server()
    srv.base_url = str(server.make_url("")).rstrip("/")  # type: ignore[attr-defined]
    try:
        yield srv
    finally:
        await server.close()


def counts_cfg(tmp_path: Path, base_url: str, watchlist_text: str, token: str | None = "tok-123456") -> AppConfig:
    s = Settings()
    s.x_api.base_url = base_url
    return AppConfig(s, load_watchlist(wl_file(tmp_path, watchlist_text)), Secrets(x_bearer_token=token),
                     tmp_path / "settings.yaml", tmp_path / "watchlist.yaml")


def test_counts_arguments() -> None:
    ns = cli.build_parser().parse_args(["counts"])
    assert (ns.days, ns.plan, ns.max_requests) == (7, False, cli.COUNTS_DEFAULT_MAX_REQUESTS)
    ns = cli.build_parser().parse_args(["counts", "--days", "3", "--plan", "--max-requests", "5"])
    assert (ns.days, ns.plan, ns.max_requests) == (3, True, 5)
    for bad in ("0", "8", "x"):
        with pytest.raises(SystemExit):
            cli.build_parser().parse_args(["counts", "--days", bad])
    assert "counts" in cli.COMMANDS


async def test_counts_command_reports_volume_and_cost(
    tmp_path: Path, fake_counts: FakeCounts, capsys: pytest.CaptureFixture[str]
) -> None:
    cfg = counts_cfg(tmp_path, fake_counts.base_url, BASE + RED_FLAG_RULE + SOURCE_RULE)  # type: ignore[attr-defined]
    args = cli.build_parser().parse_args(["counts"])
    assert await cli.cmd_counts(cfg, args) == 0
    out = capsys.readouterr().out
    assert len(fake_counts.queries) == 2
    assert all("min_followers" not in q["query"] and "start_time" not in q for q in fake_counts.queries)
    assert "2 counts queries for the last 7 day(s)" in out and "$0.010" in out
    assert "Left out of the queries (the counts endpoint rejects them): min_followers" in out
    assert "[red-flags:1] 70 posts in 7.0 days = 10.0/day" in out
    # 10 posts/day x 30 x $0.005 = $1.50 / month
    row = next(ln for ln in out.splitlines() if ln.strip().startswith("red-flags "))
    assert "red_flag" in row and "40.0" in row and "10.0" in row and "$1.50" in row and "upper bound" in row
    src_row = next(ln for ln in out.splitlines() if ln.strip().startswith("red-flag-sources "))
    assert "$1.50" in src_row and "upper bound" not in src_row
    assert "Requests: 2 counted ($0.010 at $0.005 each), 0 failed" in out


async def test_counts_days_sets_start_time(tmp_path: Path, fake_counts: FakeCounts) -> None:
    cfg = counts_cfg(tmp_path, fake_counts.base_url, BASE + RED_FLAG_RULE)  # type: ignore[attr-defined]
    assert await cli.cmd_counts(cfg, cli.build_parser().parse_args(["counts", "--days", "2"])) == 0
    start = datetime.strptime(fake_counts.queries[0]["start_time"], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC)
    assert abs((datetime.now(UTC) - start) - timedelta(days=2)) < timedelta(minutes=5)


async def test_counts_retries_without_an_operator_x_rejects(
    tmp_path: Path, fake_counts: FakeCounts, capsys: pytest.CaptureFixture[str]
) -> None:
    fake_counts.reject_operator = "has"
    text = BASE + RED_FLAG_RULE.replace("-is:retweet -is:reply lang:en", "has:links lang:en")
    cfg = counts_cfg(tmp_path, fake_counts.base_url, text)  # type: ignore[attr-defined]
    assert await cli.cmd_counts(cfg, cli.build_parser().parse_args(["counts"])) == 0
    assert [("has:links" in q["query"]) for q in fake_counts.queries] == [True, False]
    out = capsys.readouterr().out
    assert "X rejected and it was left out: has" in out and "Requests: 1 counted" in out


async def test_counts_stops_on_credits_depleted(
    tmp_path: Path, fake_counts: FakeCounts, capsys: pytest.CaptureFixture[str]
) -> None:
    fake_counts.status = 402
    cfg = counts_cfg(tmp_path, fake_counts.base_url, BASE + RED_FLAG_RULE + SOURCE_RULE)  # type: ignore[attr-defined]
    assert await cli.cmd_counts(cfg, cli.build_parser().parse_args(["counts"])) == 1
    assert len(fake_counts.queries) == 1  # every other request would fail the same way
    err = capsys.readouterr().err
    assert "credits depleted (HTTP 402)" in err and "0 of 2 queries were counted" in err


async def test_counts_plan_limits_and_token(
    tmp_path: Path, fake_counts: FakeCounts, capsys: pytest.CaptureFixture[str]
) -> None:
    text = BASE + RED_FLAG_RULE + SOURCE_RULE
    cfg = counts_cfg(tmp_path, fake_counts.base_url, text, token=None)  # type: ignore[attr-defined]
    assert await cli.cmd_counts(cfg, cli.build_parser().parse_args(["counts", "--plan"])) == 0
    assert "Nothing was sent (--plan)." in capsys.readouterr().out
    assert await cli.cmd_counts(cfg, cli.build_parser().parse_args(["counts", "--max-requests", "1"])) == 2
    assert "--max-requests is 1" in capsys.readouterr().err
    assert await cli.cmd_counts(cfg, cli.build_parser().parse_args(["counts"])) == 2  # no token
    assert "X_BEARER_TOKEN is not set" in capsys.readouterr().err
    assert fake_counts.queries == []


# --------------------------------------------------------------------------- verify: sources


def source_user(username: str, name: str, *, uid: str = "18481018", vtype: str = "government",
                followers: int = 4_000_000) -> dict[str, Any]:
    return {
        "id": uid, "username": username, "name": name, "verified": True, "verified_type": vtype,
        "protected": False, "parody": False, "created_at": "2008-12-03T00:00:00.000Z",
        "public_metrics": {"followers_count": followers, "tweet_count": 9000},
        "most_recent_tweet_id": str(datetime_to_snowflake(NOW - timedelta(hours=5)) + 7),
    }


def test_assess_source_news_words_and_regulator_badge() -> None:
    wire = Source("DeItaone", "newswire", expect_name="Walter Bloomberg")
    r = assess_source(wire, source_user("DeItaone", "Walter Bloomberg News Alerts", uid="2", vtype="blue"), NOW)
    assert r.status == "ok" and r.section == "sources"  # news words are normal for a news wire
    fan = assess_source(wire, source_user("DeItaone", "Walter Bloomberg Parody", uid="2", vtype="blue"), NOW)
    assert fan.status == "warning" and any("'parody'" in p for p in fan.problems)

    sec = Source("SECGov", "regulator", expect_name="Securities and Exchange Commission")
    ok = assess_source(sec, source_user("SECGov", "U.S. Securities and Exchange Commission"), NOW)
    assert ok.status == "ok"
    blue = assess_source(sec, source_user("SECGov", "U.S. Securities and Exchange Commission", vtype="blue"), NOW)
    assert blue.status == "warning" and "expected 'government' grey check" in blue.problems[0]
    young = source_user("SECGov", "U.S. Securities and Exchange Commission")
    young["created_at"] = (NOW - timedelta(days=20)).strftime("%Y-%m-%dT%H:%M:%S.000Z")
    assert "a real news, research or regulator account" in assess_source(sec, young, NOW).problems[0]


class FakeLookup:
    def __init__(self, users: list[dict[str, Any]]) -> None:
        self.users = users
        self.calls: list[list[str]] = []

    async def lookup_users_by_ids(self, ids: list[str], fields: list[str]) -> tuple[list[dict], list[dict]]:
        return [u for u in self.users if u["id"] in ids], []  # pinned ids of unknown handles are gone too

    async def lookup_users(self, usernames: list[str], fields: list[str]) -> tuple[list[dict], list[dict]]:
        self.calls.append(list(usernames))
        known = {u["username"].lower(): u for u in self.users}
        errors = [{"value": n, "detail": f"Could not find user with usernames: [{n}].", "title": "Not Found Error"}
                  for n in usernames if n.lower() not in known]
        return [known[n.lower()] for n in usernames if n.lower() in known], errors


SOURCES_YAML = """\
# test watchlist
accounts:
  - handle: elonmusk
    tickers: [TSLA]

# --- sources ---
sources:
  - handle: SECGov          # the regulator
    kind: regulator
    expect_name: Securities and Exchange Commission
    trusted: true
  # --- wires ---
  - handle: ghostwire
    kind: newswire
    user_id: "555"
red_flag_names: [Tesla]
keyword_rules:
  - name: red-flag-sources
    kind: source
    from: sources
    terms: "@red_flag_names"
"""


async def test_verify_sources_and_write_back_keeps_comments(tmp_path: Path) -> None:
    p = wl_file(tmp_path, SOURCES_YAML)
    wl = load_watchlist(p)
    client = FakeLookup([source_user("SECGov", "U.S. Securities and Exchange Commission")])
    results = await verify_sources(client, wl, now=NOW)  # type: ignore[arg-type]
    assert [(r.handle, r.status, r.section) for r in results] == [
        ("SECGov", "ok", "sources"), ("ghostwire", "not_found", "sources")
    ]
    assert client.calls == [["SECGov", "ghostwire"]]
    report = format_verify_report(results, title="Sources")
    assert report.startswith("Sources: 2 handle(s): 1 ok") and "@ghostwire [NOT FOUND]" in report

    # an account result with the same handle as nothing in sources never touches the sources list
    assert write_user_ids(p, [*results, VerifyResult("elonmusk", "ok", user_id="44196397")]) == 3
    text = p.read_text(encoding="utf-8")
    assert "  - handle: SECGov          # the regulator\n    user_id: \"18481018\"\n    kind: regulator" in text
    assert 'user_id: "555"' not in text and "  # --- wires ---\n  - handle: ghostwire" in text
    assert "# --- sources ---" in text and "# test watchlist" in text
    reloaded = load_watchlist(p)
    assert [s.user_id for s in reloaded.sources] == ["18481018", None]
    assert reloaded.accounts[0].user_id == "44196397"
    assert reloaded.keyword_rules[0].from_ids == ("18481018",)


async def test_cli_verify_prints_a_sources_table(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    p = wl_file(tmp_path, SOURCES_YAML)
    settings = tmp_path / "settings.yaml"
    settings.write_text(f"ops:\n  db_path: {tmp_path / 'db.sqlite'}\n", encoding="utf-8")
    users = [
        source_user("SECGov", "U.S. Securities and Exchange Commission"),
        {**source_user("elonmusk", "Elon Musk", uid="44196397", vtype="blue")},
    ]

    class Client(FakeLookup):
        def __init__(self, *a: Any, **k: Any) -> None:
            super().__init__(users)

    monkeypatch.setattr(cli, "XClient", Client)
    cfg = AppConfig(load_settings(settings), load_watchlist(p), Secrets(x_bearer_token="tok-123456"), settings, p)
    code = await cli.cmd_verify(cfg, cli.build_parser().parse_args(["verify", "--write"]))
    out = capsys.readouterr().out
    assert code == 0
    assert "Looking up 1 account(s) + 2 source(s) on X" in out
    assert "X account verification: 1 handle(s)" in out
    assert "Sources (watched only for posts about my tickers): 2 handle(s): 1 ok" in out
    assert "Updated 3 account(s)/source(s)" in out
    assert "1 of 2 source(s) now have a user_id" in out


# --------------------------------------------------------------------------- cost


def test_cost_counts_red_flag_and_source_rules(tmp_path: Path) -> None:
    wl = load_watchlist(wl_file(tmp_path, BASE + RED_FLAG_RULE + SOURCE_RULE))
    est = estimate_monthly_cost(wl, Settings())
    lines = {label: ppm for label, ppm, _ in est.lines}
    assert lines["red-flag rule red-flags"] == pytest.approx(40 * 30)
    assert lines["source rule red-flag-sources (2 sources)"] == pytest.approx(6 * 30)
    assert est.posts_per_month == pytest.approx((3 + 40 + 6) * 30)  # elonmusk default 3/day
    assert est.one_time_verify == pytest.approx((1 + 3) * 0.010)  # accounts + sources
    # worst case: a source rule's authors are its 2 sources, a red-flag rule ~30 strangers a day
    referenced = est.posts_per_month * 0.5
    authors = 1 + 30 + 2 + referenced / 30
    assert est.x_worst_case_monthly == pytest.approx(est.x_monthly + referenced * 0.005 + authors * 0.010 * 30)
    assert any("1 source(s) have no user_id yet" in n for n in est.notes)
    assert "red-flag rule red-flags" in format_cost_estimate(est)


def test_cost_leaves_out_source_rules_without_verified_sources() -> None:
    shipped = load_watchlist(WATCHLIST)
    est = estimate_monthly_cost(shipped, Settings())
    labels = [label for label, _, _ in est.lines]
    assert {"red-flag rule red-flags-mega", "red-flag rule red-flags"} <= set(labels)
    assert "source rule red-flag-sources (18 sources)" in labels  # the shipped sources are verified
    assert not any("Not streamed yet" in n for n in est.notes)
    assert any("python -m ceoscan counts" in n for n in est.notes)
    # the same watchlist before `verify --write`: the source rule is not streamed, so not counted
    unverified = replace(
        shipped,
        sources=tuple(replace(s, user_id=None) for s in shipped.sources),
        keyword_rules=tuple(replace(k, from_ids=()) if k.kind == "source" else k for k in shipped.keyword_rules),
    )
    est = estimate_monthly_cost(unverified, Settings())
    assert not any(label.startswith("source rule") for label, _, _ in est.lines)
    assert any("Not streamed yet, not counted: source rule(s) red-flag-sources" in n for n in est.notes)


def test_repo_watchlist_write_back_with_sources_only_adds_lines(tmp_path: Path) -> None:
    """write_user_ids on a copy of the shipped watchlist (which has a sources: section) only adds id lines."""
    p = tmp_path / "watchlist.yaml"
    shutil.copy(WATCHLIST, p)
    original = p.read_text(encoding="utf-8")
    wl = load_watchlist(p)
    results = [VerifyResult(a.handle, "ok", user_id=str(1000 + i)) for i, a in enumerate(wl.accounts)]
    write_user_ids(p, results)
    strip = [ln for ln in p.read_text(encoding="utf-8").splitlines() if not ln.strip().startswith("user_id:")]
    assert strip == [ln for ln in original.splitlines() if not ln.strip().startswith("user_id:")]
