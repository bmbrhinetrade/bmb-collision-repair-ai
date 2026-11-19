"""Tests for ceoscan.xapi.verify (handle verification + watchlist write-back)."""

from __future__ import annotations

import json
import shutil
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from ceoscan.config import load_watchlist
from ceoscan.models import Account, Watchlist
from ceoscan.store import Store
from ceoscan.timeutil import datetime_to_snowflake
from ceoscan.xapi.client import CreditsDepleted, NotAuthorized, RateLimited
from ceoscan.xapi.verify import (
    VERIFY_USER_FIELDS,
    VerifyResult,
    assess_user,
    batch_failure,
    format_verify_report,
    name_matches,
    record_results,
    verify_accounts,
    write_user_ids,
)

NOW = datetime(2026, 11, 19, 20, 0, tzinfo=UTC)
REPO_WATCHLIST = Path(__file__).resolve().parents[1] / "config" / "watchlist.yaml"


def make_user(
    username: str,
    name: str,
    *,
    uid: str = "44196397",
    verified: bool = True,
    verified_type: str = "blue",
    followers: int = 200_000_000,
    tweets: int = 50_000,
    protected: bool = False,
    parody: bool = False,
    last_post: datetime | None = None,
) -> dict[str, Any]:
    user: dict[str, Any] = {
        "id": uid,
        "username": username,
        "name": name,
        "verified": verified,
        "verified_type": verified_type,
        "protected": protected,
        "parody": parody,
        "public_metrics": {"followers_count": followers, "following_count": 10, "tweet_count": tweets},
        "description": "",
        "created_at": "2009-06-02T20:12:29.000Z",
    }
    lp = last_post if last_post is not None else NOW - timedelta(hours=3)
    user["most_recent_tweet_id"] = str(datetime_to_snowflake(lp) + 12345)
    return user


ELON = Account("elonmusk", ("TSLA",), kind="person", expect_name="Elon Musk")
NVIDIA = Account("nvidia", ("NVDA",), kind="company", expect_name="NVIDIA")


# --------------------------------------------------------------------------- assess_user


def test_assess_user_ok() -> None:
    r = assess_user(ELON, make_user("elonmusk", "Elon Musk"), NOW)
    assert r.status == "ok"
    assert r.problems == []
    assert r.user_id == "44196397"
    assert r.username == "elonmusk"
    assert r.name == "Elon Musk"
    assert r.verified is True and r.verified_type == "blue"
    assert r.followers == 200_000_000
    assert r.protected is False and r.parody is False
    assert r.last_post_at is not None and abs(r.last_post_at - (NOW - timedelta(hours=3))) < timedelta(seconds=1)


def test_assess_user_not_verified() -> None:
    r = assess_user(ELON, make_user("elonmusk", "Elon Musk", verified=False, verified_type="none"), NOW)
    assert r.status == "warning"
    assert r.problems == ["not verified"]
    r2 = assess_user(ELON, make_user("elonmusk", "Elon Musk", verified=True, verified_type="none"), NOW)
    assert r2.status == "warning"
    assert any("verified_type is none" in p for p in r2.problems)


def test_assess_user_company_must_be_business() -> None:
    blue = assess_user(NVIDIA, make_user("nvidia", "NVIDIA", verified_type="blue"), NOW)
    assert blue.status == "warning"
    assert len(blue.problems) == 1 and "business" in blue.problems[0] and "company account" in blue.problems[0]
    gold = assess_user(NVIDIA, make_user("nvidia", "NVIDIA", verified_type="business"), NOW)
    assert gold.status == "ok"
    # a person account with a blue check is fine
    assert assess_user(ELON, make_user("elonmusk", "Elon Musk", verified_type="blue"), NOW).status == "ok"
    # unverified company: one "not verified" problem, not a duplicate badge complaint
    unver = assess_user(NVIDIA, make_user("nvidia", "NVIDIA", verified=False, verified_type="none"), NOW)
    assert unver.problems == ["not verified"]


def test_assess_user_parody_and_protected() -> None:
    r = assess_user(ELON, make_user("elonmusk", "Elon Musk", parody=True, protected=True), NOW)
    assert r.status == "warning"
    assert "parody account" in r.problems
    assert any(p.startswith("protected account") for p in r.problems)
    assert r.parody is True and r.protected is True


def test_assess_user_low_followers() -> None:
    r = assess_user(ELON, make_user("elonmusk", "Elon Musk", followers=9_999), NOW)
    assert r.status == "warning"
    assert r.problems == ["low followers: 9,999 (< 10,000)"]
    assert assess_user(ELON, make_user("elonmusk", "Elon Musk", followers=10_000), NOW).status == "ok"


def test_assess_user_name_mismatch() -> None:
    r = assess_user(ELON, make_user("elonmusk", "Tesla Owners Club"), NOW)
    assert r.status == "warning"
    assert len(r.problems) == 1 and "does not match expected 'Elon Musk'" in r.problems[0]
    # case, punctuation, accents and emoji are ignored; every expected word must be there
    for ok_name in ("ELON MUSK", "Elon Musk ⚡️", "elon.musk!!", "Elon R. Musk", "Musk, Elon"):
        assert assess_user(ELON, make_user("elonmusk", ok_name), NOW).status == "ok", ok_name
    # one shared word is no longer enough (REQ-6): a different person with the same last name
    for bad_name in ("Musk", "Elon", "Kimbal Musk"):
        r = assess_user(ELON, make_user("elonmusk", bad_name), NOW)
        assert r.status == "warning" and "does not match expected" in r.problems[0], bad_name


@pytest.mark.parametrize(
    ("expected", "actual"),
    [
        ("Matt Schultz", "Howard Schultz"),
        ("Sam Altman", "Sam Bankman-Fried"),
        ("Lisa Su", "Lisa Smith"),
        ("Intel", "Pintel Lover"),
        ("AMD", "AMDfan"),
    ],
)
def test_name_matches_requires_every_expected_word(expected: str, actual: str) -> None:
    assert not name_matches(expected, actual)


@pytest.mark.parametrize(
    ("expected", "actual"),
    [
        ("Lip-Bu Tan", "Lip-Bu Tan"),
        ("Supermicro", "Super Micro Computer"),
        ("Open AI", "OpenAI"),
        ("Jose Perez", "José Pérez 🚀"),
        ("Micron Technology", "Micron Technology, Inc."),
    ],
)
def test_name_matches_tolerates_spacing_accents_and_suffixes(expected: str, actual: str) -> None:
    assert name_matches(expected, actual)


def test_fan_parody_and_news_words_are_flagged() -> None:
    for name in ("Elon Musk Parody Fan", "Elon Musk News", "Elon Musk (not affiliated)", "Elon Musk Updates"):
        r = assess_user(ELON, make_user("elonmusk", name), NOW)
        assert r.status == "warning", name
        assert any("fan, parody or news account?" in p for p in r.problems), name
    # an expected word is not suspicious ("Fox News" expecting "Fox News")
    news = Account("FoxNews", ("FOX",), kind="company", expect_name="Fox News")
    assert assess_user(news, make_user("FoxNews", "Fox News", verified_type="business"), NOW).status == "ok"


def test_changed_user_id_is_a_warning() -> None:
    """REQ-4 / SEC-6: a handle that now returns a different id than the pinned one is never 'ok'."""
    pinned = Account("elonmusk", ("TSLA",), kind="person", expect_name="Elon Musk", user_id="44196397")
    r = assess_user(pinned, make_user("elonmusk", "Elon Musk", uid="1999999999999999999"), NOW)
    assert r.status == "warning"
    assert r.id_changed and r.previous_user_id == "44196397" and r.user_id == "1999999999999999999"
    assert r.problems[0].startswith("user id changed from 44196397 to 1999999999999999999")
    same = assess_user(pinned, make_user("elonmusk", "Elon Musk", uid="44196397"), NOW)
    assert same.status == "ok" and not same.id_changed and same.previous_user_id is None


def test_recently_created_account_is_a_warning() -> None:
    user = make_user("elonmusk", "Elon Musk")
    user["created_at"] = (NOW - timedelta(days=18)).strftime("%Y-%m-%dT%H:%M:%S.000Z")
    r = assess_user(ELON, user, NOW)
    assert r.status == "warning"
    assert r.problems == [
        (
            f"new account: created {(NOW - timedelta(days=18)):%Y-%m-%d} (18 days ago); "
            "a real CEO/company account is usually years old (re-registered handle?)"
        )
    ]
    assert r.created_at is not None
    user["created_at"] = (NOW - timedelta(days=400)).strftime("%Y-%m-%dT%H:%M:%S.000Z")
    assert assess_user(ELON, user, NOW).status == "ok"
    user["created_at"] = "garbage"
    assert assess_user(ELON, user, NOW).status == "ok"  # unparseable dates are ignored


def test_name_matches_helper() -> None:
    assert name_matches("OpenAI", "Open AI")
    assert name_matches("Michael Saylor", "Michael Saylor⚡️")
    assert name_matches("Strategy", "Strategy")
    assert not name_matches("Elon Musk", "Doge Designer")
    assert not name_matches("The Robinhood", "The Gap")  # stopwords alone never match
    assert name_matches("", "anything")
    assert not name_matches("AMD", "Damned Deals")  # no substring matches inside other words


def test_assess_user_inactive() -> None:
    old = assess_user(ELON, make_user("elonmusk", "Elon Musk", last_post=NOW - timedelta(days=120)), NOW)
    assert old.status == "warning"
    assert len(old.problems) == 1 and old.problems[0].startswith("inactive: last post")
    assert "120 days ago" in old.problems[0]
    recent = assess_user(ELON, make_user("elonmusk", "Elon Musk", last_post=NOW - timedelta(days=89)), NOW)
    assert recent.status == "ok"
    never = make_user("elonmusk", "Elon Musk", tweets=0)
    del never["most_recent_tweet_id"]
    r = assess_user(ELON, never, NOW)
    assert r.last_post_at is None
    assert r.problems == ["inactive: account has never posted"]


def test_assess_user_multiple_problems() -> None:
    user = make_user("elonmusk", "Elon Musk Fan", verified=False, verified_type="none", followers=50, parody=True)
    r = assess_user(ELON, user, NOW)
    assert r.status == "warning"
    assert "not verified" in r.problems and "parody account" in r.problems
    assert any(p.startswith("low followers") for p in r.problems)


def test_verify_user_fields() -> None:
    for f in ("verified", "verified_type", "public_metrics", "protected", "parody", "most_recent_tweet_id"):
        assert f in VERIFY_USER_FIELDS


# --------------------------------------------------------------------------- verify_accounts


class FakeClient:
    def __init__(self, users: list[dict], errors: list[dict], exc: Exception | None = None) -> None:
        self.users = users
        self.errors = errors
        self.exc = exc
        self.calls: list[tuple[list[str], list[str]]] = []

    async def lookup_users(self, usernames: list[str], user_fields: list[str]) -> tuple[list[dict], list[dict]]:
        self.calls.append((list(usernames), list(user_fields)))
        if self.exc is not None:
            raise self.exc
        wanted = {u.lower() for u in usernames}
        return (
            [u for u in self.users if u["username"].lower() in wanted],
            [e for e in self.errors if str(e.get("value", "")).lower() in wanted],
        )


def _wl(*accounts: Account) -> Watchlist:
    return Watchlist(accounts=accounts, keyword_rules=(), my_tickers=("TSLA",), company_names={})


NOT_FOUND = {
    "value": "ghost_ceo",
    "detail": "Could not find user with usernames: [ghost_ceo].",
    "title": "Not Found Error",
    "resource_type": "user",
    "parameter": "usernames",
    "resource_id": "ghost_ceo",
    "type": "https://api.twitter.com/2/problems/resource-not-found",
}
SUSPENDED = {
    "value": "banned_ceo",
    "detail": "User has been suspended: [banned_ceo].",
    "title": "Forbidden",
    "resource_type": "user",
    "parameter": "usernames",
    "resource_id": "banned_ceo",
    "type": "https://api.twitter.com/2/problems/resource-not-found",
}


async def test_verify_accounts_maps_users_and_errors() -> None:
    wl = _wl(
        ELON,
        Account("ghost_ceo", ("TSLA",)),
        NVIDIA,
        Account("banned_ceo", ("TSLA",)),
        Account("silent_ceo", ("TSLA",)),
    )
    client = FakeClient(
        users=[make_user("ElonMusk", "Elon Musk"), make_user("NVIDIA", "NVIDIA", uid="61559439", verified_type="blue")],
        errors=[NOT_FOUND, SUSPENDED],
    )
    results = await verify_accounts(client, wl, now=NOW)  # type: ignore[arg-type]
    assert [r.handle for r in results] == ["elonmusk", "ghost_ceo", "nvidia", "banned_ceo", "silent_ceo"]
    by = {r.handle: r for r in results}
    assert by["elonmusk"].status == "ok" and by["elonmusk"].user_id == "44196397"  # case-insensitive match
    assert by["ghost_ceo"].status == "not_found"
    assert by["ghost_ceo"].problems == ["Could not find user with usernames: [ghost_ceo]."]
    assert by["ghost_ceo"].user_id is None
    assert by["nvidia"].status == "warning" and by["nvidia"].user_id == "61559439"
    assert by["banned_ceo"].status == "suspended"
    assert by["silent_ceo"].status == "error"
    assert len(client.calls) == 1
    assert client.calls[0][1] == VERIFY_USER_FIELDS


async def test_verify_accounts_batches_by_100() -> None:
    accounts = tuple(Account(f"ceo{i}", ("TSLA",)) for i in range(150))
    users = [make_user(f"ceo{i}", f"ceo{i}", uid=str(1000 + i)) for i in range(150)]
    client = FakeClient(users=users, errors=[])
    results = await verify_accounts(client, _wl(*accounts), now=NOW)  # type: ignore[arg-type]
    assert [len(c[0]) for c in client.calls] == [100, 50]
    assert len(results) == 150
    assert results[149].user_id == "1149"


async def test_verify_accounts_api_error_marks_batch_error() -> None:
    exc = CreditsDepleted(status=402, title="CreditsDepleted", detail="Your account has no credits")
    client = FakeClient(users=[], errors=[], exc=exc)
    results = await verify_accounts(client, _wl(ELON, NVIDIA), now=NOW)  # type: ignore[arg-type]
    assert [r.status for r in results] == ["error", "error"]
    assert "402" in results[0].problems[0] and "CreditsDepleted" in results[0].problems[0]
    assert {r.error_kind for r in results} == {"credits"}


@pytest.mark.parametrize(
    ("exc", "needle"),
    [
        (CreditsDepleted(status=402, title="CreditsDepleted", detail="no credits"), "add credits at console.x.com"),
        (NotAuthorized(status=401, title="Unauthorized"), "check X_BEARER_TOKEN"),
        (NotAuthorized(status=403, title="Forbidden"), "HTTP 403"),
        (RateLimited(status=429, title="Too Many Requests"), "wait up to 15 minutes"),
        (TimeoutError(), "could not reach the X API (TimeoutError)"),
    ],
)
async def test_batch_failure_collapses_global_errors_into_one_message(exc: Exception, needle: str) -> None:
    """REQ-7: one clear line instead of 31 identical errors."""
    accounts = tuple(Account(f"ceo{i}", ("TSLA",)) for i in range(31))
    results = await verify_accounts(FakeClient(users=[], errors=[], exc=exc), _wl(*accounts), now=NOW)  # type: ignore[arg-type]
    msg = batch_failure(results)
    assert msg is not None and needle in msg
    assert msg.endswith("No handles were checked, nothing was billed and nothing was written.")
    # the report (if printed at all) lists the shared error once, not 31 times
    report = format_verify_report(results)
    assert report.count("X API lookup failed") == 1 and "31 handles [ERROR]" in report


async def test_batch_failure_none_when_some_lookups_worked() -> None:
    assert batch_failure([]) is None
    mixed = [
        VerifyResult("a", "ok", user_id="1"),
        VerifyResult("b", "error", problems=["X API lookup failed: x"], error_kind="credits"),
    ]
    assert batch_failure(mixed) is None
    assert batch_failure([VerifyResult("silent", "error", problems=["X returned no data for this handle"])]) is None


class ByIdClient(FakeClient):
    """FakeClient that can also look users up by id (GET /2/users?ids=)."""

    def __init__(self, users: list[dict], errors: list[dict], by_id: list[dict], fail_by_id: bool = False) -> None:
        super().__init__(users, errors)
        self.by_id = by_id
        self.fail_by_id = fail_by_id
        self.id_calls: list[list[str]] = []

    async def lookup_users_by_ids(self, ids: list[str], user_fields: list[str]) -> tuple[list[dict], list[dict]]:
        self.id_calls.append(list(ids))
        if self.fail_by_id:
            raise CreditsDepleted(status=402, title="CreditsDepleted")
        return [u for u in self.by_id if u["id"] in ids], []


async def test_not_found_handle_with_pinned_id_reports_rename_and_keeps_id() -> None:
    """REQ-4: a renamed account keeps its pinned user_id instead of being dropped."""
    pinned = Account("smatthewschultz", ("SBUX",), kind="person", expect_name="Matt Schultz", user_id="111")
    gone = Account("ghost_ceo", ("TSLA",), user_id="222")
    renamed = make_user("mattschultz_new", "Matt Schultz", uid="111")
    not_found = [
        {**NOT_FOUND, "value": "smatthewschultz", "detail": "Could not find user with usernames: [smatthewschultz]."},
        NOT_FOUND,
    ]
    client = ByIdClient(users=[], errors=not_found, by_id=[renamed])
    results = await verify_accounts(client, _wl(pinned, gone), now=NOW)  # type: ignore[arg-type]
    by = {r.handle: r for r in results}
    r = by["smatthewschultz"]
    assert r.status == "warning" and r.user_id == "111" and not r.id_changed
    assert "now uses @mattschultz_new (renamed)" in r.problems[0]
    assert not any(p.startswith("handle resolves to") for p in r.problems)
    assert by["ghost_ceo"].status == "not_found"  # the id itself is gone too -> removed as before
    assert client.id_calls == [["111", "222"]]


async def test_pinned_id_kept_when_id_lookup_fails() -> None:
    pinned = Account("ghost_ceo", ("TSLA",), user_id="222")
    client = ByIdClient(users=[], errors=[NOT_FOUND], by_id=[], fail_by_id=True)
    [r] = await verify_accounts(client, _wl(pinned), now=NOW)  # type: ignore[arg-type]
    assert r.status == "warning" and r.user_id == "222"
    assert "could not check the pinned user_id 222" in r.problems[-1]


async def test_changed_id_is_reported_separately_and_not_written_without_accept(watchlist_file: Path) -> None:
    """SEC-6: verify --write must never silently swap a pinned user_id."""
    wl = load_watchlist(watchlist_file)
    impostor = make_user("nvidia", "NVIDIA", uid="1999999999999999999", verified_type="business")
    client = FakeClient(users=[impostor, make_user("elonmusk", "Elon Musk")], errors=[])
    results = await verify_accounts(client, wl, now=NOW)  # type: ignore[arg-type]
    nv = next(r for r in results if r.handle == "nvidia")
    assert nv.status == "warning" and nv.id_changed
    report = format_verify_report(results)
    assert "USER ID CHANGES" in report
    assert "CHANGED @nvidia: pinned 61559439 -> X now returns 1999999999999999999" in report
    write_user_ids(watchlist_file, results)
    assert 'user_id: "61559439"' in watchlist_file.read_text(encoding="utf-8")
    write_user_ids(watchlist_file, results, accept_warnings=True)  # explicit acceptance writes it
    assert 'user_id: "1999999999999999999"' in watchlist_file.read_text(encoding="utf-8")


# --------------------------------------------------------------------------- report


def test_format_verify_report() -> None:
    results = [
        VerifyResult(
            "elonmusk", "ok", "44196397", "elonmusk", "Elon Musk", True, "blue", 220_000_000, False, False, NOW, []
        ),
        VerifyResult(
            "nvidia",
            "warning",
            "61559439",
            "nvidia",
            "NVIDIA",
            True,
            "blue",
            5_000_000,
            False,
            False,
            None,
            ["company account but verified_type is 'blue' (expected 'business' gold check)"],
        ),
        VerifyResult("ghost_ceo", "not_found", problems=["Could not find user with usernames: [ghost_ceo]."]),
        VerifyResult("banned_ceo", "suspended", problems=["User has been suspended: [banned_ceo]."]),
    ]
    text = format_verify_report(results)
    first = text.splitlines()[0]
    assert "4 handle(s)" in first and "1 ok" in first and "1 warning" in first and "1 not found" in first
    assert "1 suspended" in first
    assert "@elonmusk" in text and "44196397" in text and "220.0M" in text
    assert "Handles not found or that look wrong:" in text
    assert "@ghost_ceo [NOT FOUND]: Could not find user" in text
    assert "@banned_ceo [SUSPENDED]" in text
    assert "@nvidia [WARNING]: company account" in text
    assert "--accept-warnings" in text
    bad_section = text.split("Handles not found or that look wrong:")[1]
    assert "@elonmusk" not in bad_section


def test_format_verify_report_all_ok() -> None:
    text = format_verify_report([VerifyResult("elonmusk", "ok", "1", "elonmusk", "Elon Musk", True, "blue", 5)])
    assert "All handles look like the real, verified accounts." in text
    assert format_verify_report([]).startswith("X account verification: 0 handle(s)")


# --------------------------------------------------------------------------- record_results


def test_record_results_upserts_accounts() -> None:
    store = Store(":memory:")
    n = record_results(
        store,
        [
            VerifyResult("ElonMusk", "ok", "44196397", "elonmusk", "Elon Musk", True, "blue", 10, False, False),
            VerifyResult("ghost_ceo", "not_found", problems=["Could not find user"]),
        ],
    )
    assert n == 2
    rows = {r["handle"]: dict(r) for r in store._db.execute("SELECT * FROM accounts").fetchall()}
    assert rows["elonmusk"]["user_id"] == "44196397"
    assert rows["elonmusk"]["verified"] == 1 and rows["elonmusk"]["status"] == "ok"
    assert rows["ghost_ceo"]["user_id"] is None and rows["ghost_ceo"]["status"] == "not_found"
    assert json.loads(rows["ghost_ceo"]["problems"]) == ["Could not find user"]
    store.close()


# --------------------------------------------------------------------------- write_user_ids

WATCHLIST_YAML = """\
# ceoscan watchlist - test copy
# user_id is filled in by verify --write

ticker_groups:
  bitcoin: [MSTR, CLSK]   # group comment

my_tickers: [APMD]

accounts:
  # --- Tesla ---------------------------------------------------------------
  - handle: elonmusk
    kind: person
    expect_name: Elon Musk
    tickers: [TSLA, SPCX]
    include_replies: true            # Elon replies a lot
    est_posts_per_day: 100

  # --- NVIDIA --------------------------------------------------------------
  - handle: nvidia
    kind: company
    expect_name: NVIDIA
    tickers: [NVDA]
    est_posts_per_day: 5
    user_id: "61559439"

  # --- Ghost (renamed account) ---------------------------------------------
  - handle: ghost_ceo
    tickers: ["@bitcoin"]
    user_id: "999"

  # --- Last ----------------------------------------------------------------
  - handle: saylor
    kind: person
    expect_name: "Michael Saylor"
    tickers: ["@bitcoin"]
# end of accounts

company_names:
  TSLA: [Tesla, "Model Y", FSD]
"""


@pytest.fixture
def watchlist_file(tmp_path: Path) -> Path:
    p = tmp_path / "watchlist.yaml"
    p.write_text(WATCHLIST_YAML, encoding="utf-8")
    return p


def _comment_lines(text: str) -> list[str]:
    return [ln.strip() for ln in text.splitlines() if ln.strip().startswith("#")]


def test_write_user_ids_round_trip_preserves_comments(watchlist_file: Path) -> None:
    results = [
        VerifyResult("elonmusk", "ok", user_id="44196397"),
        VerifyResult("nvidia", "warning", user_id="61559439"),  # unchanged id
        VerifyResult("ghost_ceo", "not_found", problems=["Could not find user"]),
        VerifyResult("saylor", "warning", user_id="244647486"),  # not accepted -> not written
    ]
    changed = write_user_ids(watchlist_file, results)
    assert changed == 2  # elonmusk added, ghost_ceo removed
    text = watchlist_file.read_text(encoding="utf-8")

    # every comment survived, in order, including EOL comments and section headers
    assert _comment_lines(text) == _comment_lines(WATCHLIST_YAML)
    assert "include_replies: true            # Elon replies a lot" in text
    assert "bitcoin: [MSTR, CLSK]   # group comment" in text
    assert 'expect_name: "Michael Saylor"' in text  # quotes preserved
    assert 'TSLA: [Tesla, "Model Y", FSD]' in text  # flow style preserved
    # user_id inserted right after handle, as a quoted string
    assert '  - handle: elonmusk\n    user_id: "44196397"\n    kind: person' in text
    # removed id, but the next section header is still right before the next account
    assert 'user_id: "999"' not in text
    assert '    tickers: ["@bitcoin"]\n\n  # --- Last ---' in text
    assert "saylor\n    user_id" not in text

    wl = load_watchlist(watchlist_file)
    ids = {a.handle: a.user_id for a in wl.accounts}
    assert ids == {"elonmusk": "44196397", "nvidia": "61559439", "ghost_ceo": None, "saylor": None}

    # idempotent: nothing changes the second time, and the file is not rewritten
    before = watchlist_file.read_text(encoding="utf-8")
    assert write_user_ids(watchlist_file, results) == 0
    assert watchlist_file.read_text(encoding="utf-8") == before


def test_write_user_ids_accept_warnings_and_updates(watchlist_file: Path) -> None:
    results = [
        VerifyResult("saylor", "warning", user_id="244647486"),  # last key of the last account
        VerifyResult("nvidia", "ok", user_id="12345"),  # id changed
        VerifyResult("elonmusk", "error", problems=["X API lookup failed"]),  # untouched
    ]
    assert write_user_ids(watchlist_file, results, accept_warnings=True) == 2
    text = watchlist_file.read_text(encoding="utf-8")
    assert _comment_lines(text) == _comment_lines(WATCHLIST_YAML)
    assert '  - handle: saylor\n    user_id: "244647486"\n' in text
    assert '    tickers: ["@bitcoin"]\n# end of accounts\n' in text
    assert 'user_id: "12345"' in text and "61559439" not in text
    ids = {a.handle: a.user_id for a in load_watchlist(watchlist_file).accounts}
    assert ids["saylor"] == "244647486" and ids["nvidia"] == "12345" and ids["elonmusk"] is None


def test_write_user_ids_suspended_removes_last_key_keeps_comment(tmp_path: Path) -> None:
    p = tmp_path / "w.yaml"
    p.write_text(
        "accounts:\n"
        "  - handle: a_ceo\n"
        "    tickers: [TSLA]   # eol\n"
        '    user_id: "111"\n'
        "\n"
        "  # --- section B ---\n"
        "  - handle: b_ceo\n"
        "    tickers: [TSLA]\n",
        encoding="utf-8",
    )
    assert write_user_ids(p, [VerifyResult("a_ceo", "suspended", problems=["suspended"])]) == 1
    assert p.read_text(encoding="utf-8") == (
        "accounts:\n"
        "  - handle: a_ceo\n"
        "    tickers: [TSLA]   # eol\n"
        "\n"
        "  # --- section B ---\n"
        "  - handle: b_ceo\n"
        "    tickers: [TSLA]\n"
    )


def test_write_user_ids_on_repo_watchlist_only_adds_lines(tmp_path: Path) -> None:
    """The real config/watchlist.yaml round-trips: the only change is the inserted user_id lines."""
    p = tmp_path / "watchlist.yaml"
    shutil.copy(REPO_WATCHLIST, p)
    original = p.read_text(encoding="utf-8")
    wl = load_watchlist(p)
    results = [VerifyResult(a.handle, "ok", user_id=str(1000 + i)) for i, a in enumerate(wl.accounts)]
    changed = write_user_ids(p, results)
    assert changed == sum(1 for a in wl.accounts if a.user_id != str(1000 + wl.accounts.index(a)))
    new_lines = p.read_text(encoding="utf-8").splitlines()
    removed_ids = [ln for ln in new_lines if not ln.strip().startswith("user_id:")]
    original_without_ids = [ln for ln in original.splitlines() if not ln.strip().startswith("user_id:")]
    assert removed_ids == original_without_ids
    reloaded = load_watchlist(p)
    assert [a.user_id for a in reloaded.accounts] == [str(1000 + i) for i in range(len(wl.accounts))]
