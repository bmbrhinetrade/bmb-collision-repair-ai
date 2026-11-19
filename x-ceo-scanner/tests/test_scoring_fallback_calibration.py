"""End-to-end calibration of the rules-only score: FastRules -> fallback_score.

Regression tests for the review findings: a reply or quote whose own words carry no news must
not become an "urgent" rules-only alert, rules-only scores are capped at
scoring.fallback_max_impact, and a watched treasury account's own bitcoin purchase is credited
to its ticker.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from ceoscan.config import ScoringSettings
from ceoscan.models import Account, Post, ReferencedPost, Watchlist
from ceoscan.scoring.claude import fallback_score
from ceoscan.scoring.rules import FastRules

NOW = datetime(2026, 11, 19, 20, 0, tzinfo=UTC)
CAP = ScoringSettings().fallback_max_impact
THRESHOLD = ScoringSettings().threshold

ELON = Account(handle="elonmusk", tickers=("TSLA", "SPCX"), user_id="44196397")
SAYLOR = Account(handle="saylor", tickers=("MSTR",), user_id="244647486")
SAMA = Account(handle="sama", tickers=("MSFT", "NVDA"), user_id="1605")
SATYA = Account(handle="satyanadella", tickers=("MSFT",), user_id="20571756")
LISA = Account(handle="lisasu", tickers=("AMD",), user_id="12")


@pytest.fixture(scope="module")
def rules() -> FastRules:
    return FastRules(
        Watchlist(
            accounts=(ELON, SAYLOR, SAMA, SATYA, LISA),
            keyword_rules=(),
            my_tickers=("TSLA", "SPCX", "MSTR", "MSFT", "NVDA", "AMD", "COIN", "GOOGL", "META"),
            company_names={
                "TSLA": ("Tesla", "Cybercab", "Optimus", "FSD"),
                "SPCX": ("SpaceX", "Starship"),
                "MSTR": ("MicroStrategy", "Strategy"),
                "MSFT": ("Microsoft", "Azure", "Copilot"),
                "NVDA": ("Nvidia",),
                "AMD": ("AMD", "Instinct"),
                "COIN": ("Coinbase", "Base"),
                "GOOGL": ("Google", "Gemini"),
                "META": ("Meta", "Llama"),
            },
        )
    )


def post(text: str, refs: tuple[tuple[str, str], ...] = ()) -> Post:
    return Post(
        id="1990000000000000000",
        text=text,
        author_id="1",
        created_at=NOW,
        received_at=NOW,
        source="stream",
        referenced=[ReferencedPost(type=t, id=str(i + 2), text=x) for i, (t, x) in enumerate(refs)],  # type: ignore[arg-type]
    )


def fb(rules: FastRules, account: Account | None, text: str, refs: tuple[tuple[str, str], ...] = ()):
    r = rules.evaluate(post(text, refs), account)
    return r, fallback_score(r, account, "timeout", max_impact=CAP)


@pytest.mark.parametrize(
    ("account", "text", "refs", "max_impact"),
    [
        # review: 8/10 "high" + SMS before
        (ELON, "On track. No change.", (("replied_to", "Is Tesla still on track for Q2 Cybercab production, or will guidance slip?"),), 4),
        # review: 10/10 "urgent" (quiet-hours wake-up, SMS, Pushover priority 1) before
        (ELON, "lol", (("quoted", "Tesla Q3 earnings: EPS beat estimates, guidance raised, $5B buyback announced"),), 5),
        (ELON, "Interesting", (("quoted", "FTC opens probe into Tesla"),), 4),
        (ELON, "🤣", (("quoted", "Report: Tesla in merger talks with xAI"),), 4),
        (ELON, "Yes", (("replied_to", "Will Tesla raise guidance?"),), 4),
        # review: MSTR 8/10 via the "Strategy" alias before
        (SAMA, "Our strategy for the next quarter: ship faster, partner more", (), 3),
        # review: 9/10 urgent via "results", "quarter" and "release notes" before
        (SATYA, "Great results from our Azure team this quarter, release notes here", (), 6),
        (SATYA, "Copilot in Outlook is now available to everyone", (), 6),
        (ELON, "FSD v14.2 release notes: 3x fewer interventions", (), 6),
    ],
)
def test_context_and_broad_words_do_not_reach_the_alert_threshold(
    rules: FastRules, account: Account, text: str, refs: tuple[tuple[str, str], ...], max_impact: int
) -> None:
    _, score = fb(rules, account, text, refs)
    assert score.impact <= max_impact, (text, score)
    assert score.impact < THRESHOLD


def test_saylor_bitcoin_buy_is_credited_to_mstr(rules: FastRules) -> None:
    # review: scored 3 (no alert) because the text names no company
    r, score = fb(rules, SAYLOR, "we acquired 1,000 BTC")
    assert score.impact == CAP == THRESHOLD  # alerts (daytime, "high"), never urgent / SMS
    assert score.tickers == ["MSTR"]
    _, named = fb(rules, SAYLOR, "MicroStrategy has acquired 21,021 BTC for ~$2.46 billion")
    assert named.impact == CAP and named.tickers == ["MSTR"]


@pytest.mark.parametrize(
    "text",
    [
        "$TSLA Tesla earnings: raising guidance, $5B buyback, acquiring Acme, SEC probe, layoffs",
        "Tesla Q3 deliveries: 497,000 vehicles, a record quarter. Raising full-year guidance.",
    ],
)
def test_strong_own_news_is_capped_not_dropped(rules: FastRules, text: str) -> None:
    r, score = fb(rules, ELON, text)
    assert r.own_rule_score > CAP  # the rules think this is big ...
    assert score.impact == CAP  # ... but rules alone never go past the cap
    assert fallback_score(r, ELON, None, max_impact=10).impact == r.own_rule_score


def test_unflagged_watchlist_post_scores_one(rules: FastRules) -> None:
    # every watched-account post is now sent to Claude; during an outage these must stay quiet
    for text in ("Good morning!", "Funny how things work out", "Wow"):
        _, score = fb(rules, ELON, text)
        assert score.impact == 1, text


def test_new_category_posts_stay_below_threshold_alone(rules: FastRules) -> None:
    # one strong own-text category from a watched account = 3 (account) + 2 + 1 (mine) = 6:
    # a hint for Claude, not an alert on keywords alone
    for account, text in (
        (LISA, "After 12 incredible years, I've decided to step down as CEO at the end of the year."),
        (ELON, "Funding secured."),
        (ELON, "Production at Giga Texas is paused due to a fire"),
    ):
        r, score = fb(rules, account, text)
        assert r.own_flagged, text
        assert score.impact == 6, (text, r)
        assert score.tickers[0] == account.tickers[0]
