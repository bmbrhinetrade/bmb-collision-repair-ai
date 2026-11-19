"""Unit tests for stage-1 fast rules (scoring/rules.py)."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from ceoscan.models import Account, Post, ReferencedPost, RuleResult, Watchlist
from ceoscan.scoring.rules import CATEGORY_ORDER, FastRules

NOW = datetime(2026, 11, 19, 17, 0, tzinfo=timezone.utc)

ELON = Account(handle="elonmusk", tickers=("TSLA", "SPCX"), user_id="44196397")
SAYLOR = Account(handle="saylor", tickers=("MSTR",), user_id="244647486")


@pytest.fixture(scope="module")
def watchlist() -> Watchlist:
    return Watchlist(
        accounts=(ELON, SAYLOR),
        keyword_rules=(),
        my_tickers=("TSLA", "SPCX", "NVDA", "AMD", "MSTR", "MU", "F", "BRK.B", "APMD"),
        company_names={
            "TSLA": ("Tesla", "Cybertruck", "Model Y"),
            "SPCX": ("SpaceX", "Starship", "Falcon 9"),
            "NVDA": ("Nvidia", "Blackwell"),
            "META": ("Meta",),
            "MSTR": ("MicroStrategy",),
            "INTC": ("18A",),
        },
    )


@pytest.fixture(scope="module")
def rules(watchlist: Watchlist) -> FastRules:
    return FastRules(watchlist)


def make_post(text: str, referenced: list[ReferencedPost] | None = None, cashtags: list[str] | None = None) -> Post:
    return Post(
        id="1990000000000000000",
        text=text,
        author_id="44196397",
        created_at=NOW,
        received_at=NOW,
        source="replay",
        author_username="elonmusk",
        referenced=referenced or [],
        cashtags=cashtags or [],
    )


def evaluate(rules: FastRules, text: str, account: Account | None = ELON, **kw) -> RuleResult:
    return rules.evaluate(make_post(text, **kw), account)


# --------------------------------------------------------------------------- each category


@pytest.mark.parametrize(
    ("text", "category"),
    [
        ("$TSLA is my largest position", "ticker"),
        ("TSLA deliveries tomorrow", "ticker"),
        ("Tesla is doing great work", "company"),
        ("Earnings call next week", "earnings"),
        ("EPS came in strong", "earnings"),
        ("Q3 was a record", "earnings"),
        ("Updating our outlook for the year", "guidance"),
        ("We now forecast higher margins", "guidance"),
        ("Agreed to acquire Acme Corp", "m_and_a"),
        ("Merger talks have concluded", "m_and_a"),
        ("This is a hostile takeover", "m_and_a"),
        ("Excited to partner with Acme", "partnership"),
        ("New collaboration with the city", "partnership"),
        ("Painful layoffs today", "layoffs"),
        ("We are restructuring the team", "layoffs"),
        ("Unveiling something new tonight", "product"),
        ("Available now in all stores", "product"),
        ("We ship next month", "product"),
        ("FDA approval received", "regulatory"),
        ("The SEC filed a complaint", "regulatory"),
        ("Antitrust lawsuit dismissed", "regulatory"),
        ("New tariffs hit imports", "regulatory"),
        ("Board authorized a share repurchase", "buyback"),
        ("Accelerating our buyback", "buyback"),
        ("Pricing our IPO this week", "offering"),
        ("Convertible notes priced", "offering"),
        ("New ATM program filed", "offering"),
        ("We plan to raise $2B in equity", "offering"),
        ("Strategy has acquired 1,000 BTC", "bitcoin"),
        ("We bought more bitcoin this week", "bitcoin"),
        ("₿ is the future", "bitcoin"),
        ("Revenue hit $5B", "numbers"),
        ("Revenue grew to $500 million", "numbers"),
        ("Shipped 1.2M units", "numbers"),
        ("Margins up 25%", "numbers"),
        ("Adding 10 GW of capacity", "numbers"),
    ],
)
def test_flags_each_category(rules: FastRules, text: str, category: str) -> None:
    result = evaluate(rules, text)
    assert result.flagged, text
    assert category in result.categories, (text, result.categories)
    assert 1 <= result.rule_score <= 10
    assert result.reasons


def test_categories_cover_contract_names() -> None:
    assert set(CATEGORY_ORDER) == {
        "ticker",
        "company",
        "earnings",
        "guidance",
        "m_and_a",
        "partnership",
        "layoffs",
        "product",
        "regulatory",
        "buyback",
        "offering",
        "bitcoin",
        "numbers",
    }


# --------------------------------------------------------------------------- chit-chat


@pytest.mark.parametrize(
    "text",
    [
        "Good morning everyone! Hope you all have a wonderful weekend 😊",
        "lol so true",
        "Exactly!!",
        "100%",
        "This is the way",
        "Wow, what a view tonight",
        "Thanks, appreciate it 🙏",
        "brb, busy atm",
        "back in a sec",
        "Interesting. Will think about it",
        "My dog thinks he is a cat",
        "Metaverse vibes",  # "Meta" alias needs a word boundary
        "Teslafication of everything",  # alias inside a longer word
        "tsla",  # lowercase bare word is not a ticker
    ],
)
def test_does_not_flag_chit_chat(rules: FastRules, text: str) -> None:
    result = evaluate(rules, text)
    assert result == RuleResult(flagged=False), (text, result)


# --------------------------------------------------------------------------- tickers


def test_one_and_two_letter_tickers_only_via_cashtag(rules: FastRules) -> None:
    bare = evaluate(rules, "MU and F reported today, so did I")
    assert "MU" not in bare.tickers and "F" not in bare.tickers
    assert "ticker" not in bare.categories

    tagged = evaluate(rules, "Watching $MU and $F closely")
    assert tagged.flagged
    assert tagged.tickers == ["MU", "F"]
    assert "cashtag $MU" in tagged.reasons and "cashtag $F" in tagged.reasons


def test_bare_ticker_needs_three_chars_and_uppercase(rules: FastRules) -> None:
    result = evaluate(rules, "NVDA and AMD are both up")
    assert result.tickers == ["NVDA", "AMD"]
    assert "ticker NVDA" in result.reasons
    assert not evaluate(rules, "NVDAX is not a ticker of mine").flagged


def test_bare_ticker_with_dot(rules: FastRules) -> None:
    assert evaluate(rules, "Buffett's BRK.B letter").tickers == ["BRK.B"]


def test_any_cashtag_flags_even_if_not_mine(rules: FastRules) -> None:
    result = evaluate(rules, "Thoughts on $pltr?")
    assert result.flagged and result.tickers == ["PLTR"]
    assert result.reasons == ["cashtag $PLTR"]


def test_dollar_amount_is_not_a_cashtag(rules: FastRules) -> None:
    result = evaluate(rules, "Raised prices by $5B overall")
    assert result.tickers == []
    assert "ticker" not in result.categories
    assert "numbers" in result.categories and result.has_numbers


def test_cashtag_at_end_of_sentence(rules: FastRules) -> None:
    assert evaluate(rules, "Long $TSLA.").tickers == ["TSLA"]


def test_tickers_deduped_and_uppercased(rules: FastRules) -> None:
    result = evaluate(rules, "$tsla $TSLA TSLA Tesla Cybertruck")
    assert result.tickers == ["TSLA"]
    assert set(result.categories) >= {"ticker", "company"}


def test_entity_cashtags_are_included(rules: FastRules) -> None:
    result = evaluate(rules, "big day", cashtags=["NVDA"])
    assert result.flagged and result.tickers == ["NVDA"]


def test_company_alias_maps_to_ticker(rules: FastRules) -> None:
    result = evaluate(rules, "the new model y and starship look amazing")
    assert result.tickers == ["TSLA", "SPCX"]
    assert "company: Model Y" in result.reasons
    assert "company: Starship" in result.reasons


def test_multiword_and_numeric_aliases(rules: FastRules) -> None:
    assert evaluate(rules, "Falcon  9 flew again").tickers == ["SPCX"]
    assert evaluate(rules, "18A yields look good").tickers == ["INTC"]


# --------------------------------------------------------------------------- context, reasons, score


def test_quoted_and_parent_text_are_scanned(rules: FastRules) -> None:
    parent = ReferencedPost(type="replied_to", id="2", text="Will Tesla update guidance?", author_username="someone")
    result = evaluate(rules, "Yes", referenced=[parent])
    assert result.flagged
    assert "company" in result.categories and "guidance" in result.categories
    assert result.tickers == ["TSLA"]


def test_referenced_post_without_text_is_ignored(rules: FastRules) -> None:
    ref = ReferencedPost(type="quoted", id="2", text=None)
    assert not evaluate(rules, "lol", referenced=[ref]).flagged


def test_bitcoin_purchase_is_not_m_and_a(rules: FastRules) -> None:
    result = evaluate(rules, "MicroStrategy has acquired 21,021 BTC for ~$2.46 billion", account=SAYLOR)
    assert "bitcoin" in result.categories
    assert "m_and_a" not in result.categories
    assert "numbers" in result.categories
    assert result.tickers == ["MSTR"]
    assert any(r.startswith("bitcoin: acquired 21,021 BTC") for r in result.reasons)


def test_plain_bitcoin_mention_is_not_a_purchase(rules: FastRules) -> None:
    assert not evaluate(rules, "Bitcoin is hope", account=SAYLOR).flagged


def test_reason_strings(rules: FastRules) -> None:
    result = evaluate(rules, "$TSLA Tesla raises guidance after $5B quarter")
    assert "cashtag $TSLA" in result.reasons
    assert "company: Tesla" in result.reasons
    assert "keyword: guidance" in result.reasons
    assert "number: $5B" in result.reasons
    # "raises guidance" is not a capital raise
    assert "offering" not in result.categories


def test_acronyms_are_case_sensitive(rules: FastRules) -> None:
    assert "regulatory" in evaluate(rules, "SEC filing").categories
    assert not evaluate(rules, "one sec").flagged
    assert "offering" in evaluate(rules, "ATM offering").categories


def test_rule_score_heuristic(rules: FastRules) -> None:
    assert evaluate(rules, "lol").rule_score == 0
    numbers_only = evaluate(rules, "up 25%")
    assert numbers_only.flagged and numbers_only.rule_score == 1
    ticker_only = evaluate(rules, "$PLTR")
    assert ticker_only.rule_score == 3  # +3 ticker, PLTR is not one of my tickers
    mine = evaluate(rules, "$TSLA")
    assert mine.rule_score == 4  # +3 ticker, +1 relevant to my tickers
    loaded = evaluate(
        rules,
        "$TSLA Tesla earnings: raising guidance, $5B buyback, acquiring Acme, SEC probe, layoffs",
    )
    assert loaded.rule_score == 10  # capped


def test_categories_reported_in_canonical_order(rules: FastRules) -> None:
    result = evaluate(rules, "25% growth: Tesla partnership with Nvidia announced")
    order = [c for c in CATEGORY_ORDER if c in result.categories]
    assert result.categories == order


def test_empty_watchlist_still_works() -> None:
    fr = FastRules(Watchlist(accounts=(), keyword_rules=(), my_tickers=(), company_names={}))
    assert fr.evaluate(make_post("$TSLA to the moon"), None).tickers == ["TSLA"]
    assert not fr.evaluate(make_post("hello world"), None).flagged
