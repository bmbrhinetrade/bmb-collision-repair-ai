"""Tests for ceoscan.ops.cost (monthly X API + Claude cost estimate)."""

from __future__ import annotations

import pytest

from ceoscan.config import Settings
from ceoscan.models import Account, KeywordRule, Watchlist
from ceoscan.ops.cost import CostEstimate, estimate_monthly_cost, format_cost_estimate


def _watchlist(*accounts: Account, keyword_rules: tuple[KeywordRule, ...] = ()) -> Watchlist:
    return Watchlist(accounts=accounts, keyword_rules=keyword_rules, my_tickers=("TSLA", "AMD"), company_names={})


@pytest.fixture
def wl() -> Watchlist:
    return _watchlist(
        Account("elonmusk", ("TSLA",), est_posts_per_day=10, user_id="44196397"),
        Account("LisaSu", ("AMD",), est_posts_per_day=2),
        keyword_rules=(KeywordRule("big-accounts", ("TSLA", "AMD"), est_posts_per_day=60),),
    )


def test_stream_cost_math(wl: Watchlist) -> None:
    est = estimate_monthly_cost(wl, Settings())
    assert isinstance(est, CostEstimate)
    # 30 days * (10 + 2 + 60) posts/day
    assert est.posts_per_month == pytest.approx(2160)
    assert est.x_monthly == pytest.approx(2160 * 0.005)  # $10.80
    # worst case: + 50% referenced posts * post_read + (2 accounts + 30 keyword authors) * user_read * 30
    assert est.x_worst_case_monthly == pytest.approx(10.80 + 1080 * 0.005 + 32 * 0.010 * 30)  # $25.80
    # Claude: posts * flag_rate * (900 * $0.10 + 300 * $0.50) / 1e6
    assert est.claude_monthly == pytest.approx(2160 * 0.5 * (900 * 0.10 + 300 * 0.50) / 1e6)
    assert est.one_time_verify == pytest.approx(2 * 0.010)
    assert est.cap_ok is True
    assert est.mode == "stream"
    assert est.lines == [
        ("@elonmusk", pytest.approx(300), pytest.approx(1.5)),
        ("@LisaSu", pytest.approx(60), pytest.approx(0.3)),
        ("keyword rule big-accounts", pytest.approx(1800), pytest.approx(9.0)),
    ]
    assert est.total_monthly == pytest.approx(est.x_monthly + est.claude_monthly)


def test_poll_mode_excludes_keyword_rules(wl: Watchlist) -> None:
    est = estimate_monthly_cost(wl, Settings(), mode="poll")
    assert est.mode == "poll"
    assert est.posts_per_month == pytest.approx(360)
    assert est.x_monthly == pytest.approx(1.80)
    assert est.x_worst_case_monthly == pytest.approx(1.80 + 180 * 0.005 + 2 * 0.010 * 30)
    assert all(not label.startswith("keyword") for label, _, _ in est.lines)
    assert any("keyword rule" in n and "not counted" in n for n in est.notes)


def test_pricing_comes_from_settings(wl: Watchlist) -> None:
    s = Settings()
    s.x_api.pricing.post_read = 0.01
    s.x_api.pricing.user_read = 0.02
    s.x_api.pricing.checked = "2027-01-02"
    s.scoring.pricing.input_per_mtok = 1.0
    s.scoring.pricing.output_per_mtok = 5.0
    est = estimate_monthly_cost(wl, s, flag_rate=1.0, avg_input_tokens=1000, avg_output_tokens=100)
    assert est.x_monthly == pytest.approx(21.60)
    assert est.one_time_verify == pytest.approx(0.04)
    assert est.claude_monthly == pytest.approx(2160 * (1000 * 1.0 + 100 * 5.0) / 1e6)
    assert any("2027-01-02" in n and "est_posts_per_day" in n for n in est.notes)


def test_cap_exceeded_and_scoring_disabled() -> None:
    wl = _watchlist(Account("firehose", ("TSLA",), est_posts_per_day=200_000))
    s = Settings()
    s.scoring.enabled = False
    est = estimate_monthly_cost(wl, s)
    assert est.posts_per_month == pytest.approx(6_000_000)
    assert est.cap_ok is False
    assert est.claude_monthly == 0.0
    assert any("exceeds" in n and "3,000,000" in n for n in est.notes)
    assert any("disabled" in n for n in est.notes)
    assert "OVER the 3,000,000 monthly cap" in format_cost_estimate(est)


def test_worst_case_over_cap_note() -> None:
    wl = _watchlist(Account("busy", ("TSLA",), est_posts_per_day=80_000))  # 2.4M/month, worst 3.6M
    est = estimate_monthly_cost(wl, Settings())
    assert est.cap_ok is True
    assert any("Worst case" in n and "exceed" in n for n in est.notes)


def test_notes_flag_missing_user_ids_and_largest_source(wl: Watchlist) -> None:
    est = estimate_monthly_cost(wl, Settings())
    assert any("1 account(s) have no user_id" in n for n in est.notes)
    assert any(n.startswith("Largest source: keyword rule big-accounts") for n in est.notes)


def test_flag_rate_validation(wl: Watchlist) -> None:
    with pytest.raises(ValueError):
        estimate_monthly_cost(wl, Settings(), flag_rate=1.5)


def test_empty_watchlist() -> None:
    est = estimate_monthly_cost(_watchlist(), Settings())
    assert est.posts_per_month == 0
    assert est.x_monthly == 0
    assert est.x_worst_case_monthly == 0
    assert est.one_time_verify == 0
    assert "Estimated monthly cost" in format_cost_estimate(est)


def test_format_cost_estimate(wl: Watchlist) -> None:
    text = format_cost_estimate(estimate_monthly_cost(wl, Settings()))
    assert text.splitlines()[0] == "Estimated monthly cost (stream mode)"
    assert "$10.80 / month" in text  # X post reads
    assert "$25.80 / month" in text  # worst case
    assert "2,160 / month (within the 3,000,000 monthly cap)" in text
    assert "$0.005 per post read, $0.010 per user read" in text
    assert "est_posts_per_day from the watchlist" in text
    assert "checked 2026-11-19" in text
    # sources sorted by cost, the biggest first
    src = [ln.strip() for ln in text.splitlines() if "posts/mo" in ln]
    assert src[0].startswith("keyword rule big-accounts")
    assert src[1].startswith("@elonmusk")


def test_format_truncates_long_source_list() -> None:
    accounts = tuple(Account(f"acct{i}", ("TSLA",), est_posts_per_day=i + 1) for i in range(20))
    text = format_cost_estimate(estimate_monthly_cost(_watchlist(*accounts), Settings()))
    assert "... 8 more:" in text
