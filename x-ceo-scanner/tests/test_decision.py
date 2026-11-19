"""decide(): threshold, quiet hours (overnight window), always-alert accounts, jokes, my-ticker filter."""

from __future__ import annotations

from datetime import UTC, datetime
from zoneinfo import ZoneInfo

import pytest

from ceoscan.alerts.decision import decide, priority_for
from ceoscan.config import Settings
from ceoscan.models import Account, RuleResult, Score

PT = ZoneInfo("America/Los_Angeles")
MY = ("TSLA", "SPCX", "NVDA")
FLAGGED = RuleResult(flagged=True, reasons=["cashtag $TSLA"], categories=["ticker"], tickers=["TSLA"], rule_score=5)
UNFLAGGED = RuleResult(flagged=False)

ELON = Account(handle="elonmusk", tickers=("TSLA", "SPCX"), user_id="44196397")
VIP = Account(handle="saylor", tickers=("MSTR",), user_id="244647486", always_alert=True)


def pt(hour: int, minute: int = 0, day: int = 19, month: int = 11) -> datetime:
    """A local Pacific time on 2026-<month>-<day>, returned in UTC (like the pipeline clock)."""
    return datetime(2026, month, day, hour, minute, tzinfo=PT).astimezone(UTC)


DAY = pt(12)  # noon PT: never quiet
NIGHT = pt(23)  # inside the default 22:00-06:30 window


def score(impact: int, tickers: list[str] | None = None, joke: bool = False, direction: str = "bullish") -> Score:
    return Score(
        tickers=["TSLA"] if tickers is None else tickers,
        direction=direction,  # type: ignore[arg-type]
        impact=impact,
        reason="test",
        is_joke=joke,
        source="claude",
    )


@pytest.fixture
def settings() -> Settings:
    return Settings()  # threshold 7, quiet 22:00-06:30 at >= 9, joke 9, require_my_ticker True


# --------------------------------------------------------------------------- no score


def test_score_none_is_not_flagged(settings: Settings) -> None:
    d = decide(None, UNFLAGGED, ELON, settings, DAY, MY)
    assert not d.should_alert
    assert d.reason == "not flagged"


def test_score_none_for_always_alert_account_never_alerts(settings: Settings) -> None:
    d = decide(None, UNFLAGGED, VIP, settings, DAY, MY)
    assert not d.should_alert


# --------------------------------------------------------------------------- threshold


@pytest.mark.parametrize(
    ("impact", "alert", "priority"),
    [(1, False, "normal"), (6, False, "normal"), (7, True, "high"), (8, True, "high"), (9, True, "urgent"),
     (10, True, "urgent")],
)
def test_threshold_daytime(settings: Settings, impact: int, alert: bool, priority: str) -> None:
    d = decide(score(impact), FLAGGED, ELON, settings, DAY, MY)
    assert d.should_alert is alert
    assert d.quiet_hours is False
    if alert:
        assert d.priority == priority
        assert d.reason == f"score {impact} >= 7"
    else:
        assert d.reason == f"below threshold: {impact} < 7"


def test_custom_threshold_lower_scores_are_normal_priority(settings: Settings) -> None:
    settings.scoring.threshold = 5
    d = decide(score(5), FLAGGED, ELON, settings, DAY, MY)
    assert d.should_alert and d.priority == "normal"
    assert d.reason == "score 5 >= 5"


def test_account_none_uses_regular_rules(settings: Settings) -> None:
    assert decide(score(7), FLAGGED, None, settings, DAY, MY).should_alert
    assert not decide(score(6), FLAGGED, None, settings, DAY, MY).should_alert


def test_priority_for() -> None:
    assert [priority_for(i) for i in (1, 6, 7, 8, 9, 10)] == ["normal", "normal", "high", "high", "urgent", "urgent"]


# --------------------------------------------------------------------------- quiet hours


def test_quiet_hours_blocks_below_quiet_threshold(settings: Settings) -> None:
    d = decide(score(8), FLAGGED, ELON, settings, NIGHT, MY)
    assert not d.should_alert
    assert d.quiet_hours is True
    assert d.reason == "quiet hours: 8 < 9"


def test_quiet_hours_allows_quiet_threshold(settings: Settings) -> None:
    d = decide(score(9), FLAGGED, ELON, settings, NIGHT, MY)
    assert d.should_alert and d.quiet_hours and d.priority == "urgent"
    assert "9 >= 9" in d.reason


@pytest.mark.parametrize(
    ("when", "quiet"),
    [
        (pt(21, 59), False),
        (pt(22, 0), True),  # start inclusive
        (pt(23, 59), True),
        (pt(0, 0), True),  # after midnight (overnight window)
        (pt(3, 15), True),
        (pt(6, 29), True),
        (pt(6, 30), False),  # end exclusive
        (pt(9, 0), False),
        (pt(23, 0, day=15, month=7), True),  # PDT (summer) is handled too
        (pt(7, 0, day=15, month=7), False),
    ],
)
def test_quiet_window_overnight(settings: Settings, when: datetime, quiet: bool) -> None:
    d = decide(score(8), FLAGGED, ELON, settings, when, MY)
    assert d.quiet_hours is quiet
    assert d.should_alert is (not quiet)


def test_quiet_hours_disabled(settings: Settings) -> None:
    settings.alerts.quiet_hours.enabled = False
    d = decide(score(8), FLAGGED, ELON, settings, NIGHT, MY)
    assert d.should_alert and not d.quiet_hours


def test_daytime_quiet_window(settings: Settings) -> None:
    settings.alerts.quiet_hours.start = "09:00"
    settings.alerts.quiet_hours.end = "17:00"
    assert decide(score(8), FLAGGED, ELON, settings, pt(12), MY).quiet_hours
    assert not decide(score(8), FLAGGED, ELON, settings, pt(23), MY).quiet_hours


# --------------------------------------------------------------------------- always-alert accounts


def test_always_alert_low_score_daytime(settings: Settings) -> None:
    d = decide(score(3, tickers=["MSTR"]), FLAGGED, VIP, settings, DAY, MY)
    assert d.should_alert
    assert d.priority == "normal"
    assert d.reason == "always-alert account @saylor"


def test_always_alert_ignores_joke_and_ticker_filters(settings: Settings) -> None:
    d = decide(score(2, tickers=[], joke=True), UNFLAGGED, VIP, settings, DAY, MY)
    assert d.should_alert


@pytest.mark.parametrize(("impact", "priority"), [(7, "high"), (9, "urgent")])
def test_always_alert_priority_follows_score(settings: Settings, impact: int, priority: str) -> None:
    assert decide(score(impact), FLAGGED, VIP, settings, DAY, MY).priority == priority


def test_always_alert_quiet_hours_without_bypass_blocks_low(settings: Settings) -> None:
    d = decide(score(5), FLAGGED, VIP, settings, NIGHT, MY)
    assert not d.should_alert
    assert d.quiet_hours
    assert d.reason.startswith("quiet hours: 5 < 9")
    assert "@saylor" in d.reason


def test_always_alert_quiet_hours_without_bypass_allows_high(settings: Settings) -> None:
    d = decide(score(9), FLAGGED, VIP, settings, NIGHT, MY)
    assert d.should_alert and d.quiet_hours and d.priority == "urgent"


def test_always_alert_quiet_hours_with_bypass(settings: Settings) -> None:
    settings.alerts.quiet_hours.always_alert_bypass = True
    d = decide(score(3), FLAGGED, VIP, settings, NIGHT, MY)
    assert d.should_alert
    assert d.quiet_hours
    assert "bypass" in d.reason


def test_bypass_does_not_apply_to_regular_accounts(settings: Settings) -> None:
    settings.alerts.quiet_hours.always_alert_bypass = True
    assert not decide(score(8), FLAGGED, ELON, settings, NIGHT, MY).should_alert


# --------------------------------------------------------------------------- jokes


def test_joke_needs_joke_threshold(settings: Settings) -> None:
    d = decide(score(8, joke=True), FLAGGED, ELON, settings, DAY, MY)
    assert not d.should_alert
    assert d.reason == "joke/meme: 8 < 9"


def test_joke_at_joke_threshold_alerts(settings: Settings) -> None:
    d = decide(score(9, joke=True), FLAGGED, ELON, settings, DAY, MY)
    assert d.should_alert
    assert "joke" in d.reason


def test_joke_threshold_configurable(settings: Settings) -> None:
    settings.scoring.joke_threshold = 7
    assert decide(score(7, joke=True), FLAGGED, ELON, settings, DAY, MY).should_alert


# --------------------------------------------------------------------------- require_my_ticker


def test_require_my_ticker_blocks_unrelated_tickers(settings: Settings) -> None:
    d = decide(score(8, tickers=["XYZ"]), FLAGGED, ELON, settings, DAY, MY)
    assert not d.should_alert
    assert "no watched ticker" in d.reason and "XYZ" in d.reason


def test_require_my_ticker_blocks_empty_tickers(settings: Settings) -> None:
    assert not decide(score(9, tickers=[]), FLAGGED, ELON, settings, DAY, MY).should_alert


def test_require_my_ticker_any_overlap_and_case_insensitive(settings: Settings) -> None:
    assert decide(score(8, tickers=["XYZ", "$nvda"]), FLAGGED, ELON, settings, DAY, MY).should_alert


def test_require_my_ticker_disabled(settings: Settings) -> None:
    settings.scoring.require_my_ticker = False
    assert decide(score(8, tickers=["XYZ"]), FLAGGED, ELON, settings, DAY, MY).should_alert
    assert decide(score(8, tickers=[]), FLAGGED, ELON, settings, DAY, MY).should_alert
