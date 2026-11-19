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
        "exec_change",
        "partnership",
        "layoffs",
        "product",
        "regulatory",
        "clinical",
        "fda_action",
        "corporate_status",
        "incident",
        "buyback",
        "capital_return",
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


# --------------------------------------------------------------------------- new categories (SCOR-3)


@pytest.mark.parametrize(
    ("text", "category"),
    [
        # executive change
        ("After 12 incredible years, I've decided to step down as CEO at the end of the year.", "exec_change"),
        ("I am stepping down as CEO of Tesla", "exec_change"),
        ("Our CFO has resigned", "exec_change"),
        ("Please welcome our new CEO", "exec_change"),
        ("Jane Doe was appointed as chief financial officer", "exec_change"),
        ("She will be my successor", "exec_change"),
        ("Personal news: after many wonderful years it is time for me to pass the baton.", "exec_change"),
        # capital return
        ("10-for-1 stock split effective June 7", "capital_return"),
        ("Board declared a special dividend of $1 per share", "capital_return"),
        ("We are initiating a quarterly dividend", "capital_return"),
        ("1-for-10 reverse split approved", "capital_return"),
        # take-private, with words in between, and the famous two-word post
        ("Am considering taking it private at $420.", "m_and_a"),
        ("Am considering taking Tesla private at $420.", "m_and_a"),
        ("Funding secured.", "m_and_a"),
        ("We could go private", "m_and_a"),
        # incidents
        ("Our systems are down right now", "incident"),
        ("We are aware of an outage affecting some users", "incident"),
        ("Production at Giga Texas is paused due to a fire", "incident"),
        ("We halted production at the plant", "incident"),
        ("Voluntary recall of 2,000 vehicles", "incident"),
        ("Our database was hacked last night", "incident"),
        ("We detected a data breach", "incident"),
        ("Trading halted pending news", "incident"),
        # bankruptcy / delisting / charter / license
        ("We received our bank charter", "corporate_status"),
        ("The company filed for Chapter 11 bankruptcy", "corporate_status"),
        ("Shares will be delisted from Nasdaq", "corporate_status"),
        ("We were granted a banking license in the EU", "corporate_status"),
        # bitcoin sales
        ("We sold some bitcoin this week to fund the dividend.", "bitcoin"),
        ("Strategy is selling 704 BTC", "bitcoin"),
        # product launches ("introducing")
        ("Today we're introducing the all-new MacBook Pro", "product"),
    ],
)
def test_flags_new_categories(rules: FastRules, text: str, category: str) -> None:
    result = evaluate(rules, text)
    assert result.flagged, text
    assert category in result.categories, (text, result.categories)


def test_bitcoin_sale_reason_and_hodl_posts(rules: FastRules) -> None:
    sale = evaluate(rules, "We sold some bitcoin this week to fund the dividend.", account=SAYLOR)
    assert any(r.startswith("bitcoin sale: sold some bitcoin") for r in sale.reasons)
    assert "capital_return" in sale.categories  # "the dividend" (broad)
    for text in (
        "Never sell your bitcoin",
        "Don't sell your ₿itcoin... just kidding, don't sell your bitcoin",
        "I have no plans to sell bitcoin",
        "Sell a kidney if you must, but keep the bitcoin",
    ):
        assert not any(r.startswith("bitcoin sale") for r in evaluate(rules, text, account=SAYLOR).reasons), text


@pytest.mark.parametrize(
    "text",
    [
        "take your private jet",  # not a take-private
        "Revenue from services was down 3% year over year",  # a metric, not an outage
        "Services were down 3% yoy",
        "This feature is enabled by default on all devices",  # not a debt default
        "Chapter 7 of my new book is out",
        "The successor to last year's model ships soon",  # not an executive successor
        "The next President will be decided in November",  # politics, not a company change
        "BTC breached $100,000",  # a price level, not a security breach
    ],
)
def test_new_categories_do_not_score_common_phrases(rules: FastRules, text: str) -> None:
    result = evaluate(rules, text)
    for cat in ("m_and_a", "incident", "corporate_status", "exec_change"):
        if cat in result.categories:
            # at most a broad (unscored) match: the rules-only score ignores it
            assert result.rule_score <= 2, (text, result)


# --------------------------------------------------------------------------- own text vs context (SCOR-1)


def test_own_text_is_evaluated_separately_from_context(rules: FastRules) -> None:
    parent = ReferencedPost(
        type="replied_to", id="2", text="Is Tesla still on track for Q2 Cybercab production, or will guidance slip?"
    )
    result = evaluate(rules, "On track. No change.", referenced=[parent])
    assert result.flagged and "guidance" in result.categories and result.tickers == ["TSLA"]
    assert result.own_flagged is False
    assert result.own_categories == []
    assert result.own_rule_score == 0


def test_own_fields_equal_full_fields_without_context(rules: FastRules) -> None:
    result = evaluate(rules, "$TSLA Tesla raises guidance after $5B quarter")
    assert result.own_flagged is True
    assert result.own_categories == result.categories
    assert result.own_rule_score == result.rule_score


def test_quote_with_own_words(rules: FastRules) -> None:
    quoted = ReferencedPost(type="quoted", id="2", text="Tesla Q3 earnings: EPS beat estimates, $5B buyback")
    result = evaluate(rules, "Raising full-year guidance too", referenced=[quoted])
    assert result.own_categories == ["guidance"]
    assert result.own_flagged and 1 <= result.own_rule_score < result.rule_score


def test_repost_text_counts_as_own_text(rules: FastRules) -> None:
    original = ReferencedPost(type="retweeted", id="2", text="Tesla raises full-year guidance")
    result = evaluate(rules, "RT @Tesla: Tesla raises full-year", referenced=[original])
    assert "guidance" in result.own_categories


def test_watched_account_strong_own_news_counts_its_tickers(rules: FastRules) -> None:
    # "we acquired 1,000 BTC" names no company, but from @saylor it is about MSTR
    saylor = evaluate(rules, "we acquired 1,000 BTC", account=SAYLOR)
    named = evaluate(rules, "MicroStrategy has acquired 1,000 BTC", account=SAYLOR)
    assert saylor.tickers == []  # detected tickers stay what the text says
    assert saylor.own_rule_score == named.own_rule_score == 7  # +3 account, +2 bitcoin, +1 numbers, +1 mine
    # the same text from a non-watched author gets no ticker credit
    assert evaluate(rules, "we acquired 1,000 BTC", account=None).rule_score == 3
    # weak categories alone do not trigger the account credit
    assert evaluate(rules, "Launching something tonight", account=ELON).rule_score == 1


# --------------------------------------------------------------------------- common-word aliases (SCOR-4)


@pytest.fixture(scope="module")
def alias_rules() -> FastRules:
    return FastRules(
        Watchlist(
            accounts=(ELON, SAYLOR),
            keyword_rules=(),
            my_tickers=("TSLA", "MSTR", "COIN", "META", "AMD", "GOOGL", "MSFT"),
            company_names={
                "MSTR": ("MicroStrategy", "Strategy"),
                "COIN": ("Coinbase", "Base"),
                "META": ("Meta", "Llama", "Meta Quest"),
                "AMD": ("AMD", "Instinct"),
                "GOOGL": ("Google", "Gemini", "=Gaudi"),
                "MSFT": ("Microsoft", "Azure", "Copilot"),
            },
        )
    )


@pytest.mark.parametrize(
    "text",
    [
        "Our strategy is simple: build great products",
        "Strategy is everything.",  # first word of a sentence
        "Great quarter. Strategy matters",
        "The base case is that crypto keeps growing",
        "Back to base.",
        "Base case: we keep shipping",
        "So meta",
        "Trust your instinct",
        "my AI copilot",
        "the azure sky",
        "a llama farm",
        "gaudi architecture",  # "=Gaudi" marks an alias exact-case
    ],
)
def test_common_word_aliases_do_not_match_everyday_text(alias_rules: FastRules, text: str) -> None:
    result = alias_rules.evaluate(make_post(text), ELON)
    assert "company" not in result.categories, (text, result.reasons)
    assert result.tickers == []


@pytest.mark.parametrize(
    ("text", "ticker"),
    [
        ("Strategy has acquired 1,000 BTC", None),  # sentence start: credited via the account instead
        ("Today Strategy acquired 1,000 BTC", "MSTR"),
        ("We are building on Base", "COIN"),
        ("Meta is hiring", "META"),
        ("New Meta Quest headset", "META"),
        ("meta quest headsets", "META"),  # the multi-word alias stays case-insensitive
        ("Instinct MI350 shipping", "AMD"),
        ("Copilot is now in Word", "MSFT"),
        ("microsoft and coinbase", "MSFT"),  # ordinary aliases stay case-insensitive
        ("Gaudi chips", "GOOGL"),
    ],
)
def test_aliases_still_match_when_capitalized(alias_rules: FastRules, text: str, ticker: str | None) -> None:
    result = alias_rules.evaluate(make_post(text), ELON)
    if ticker is None:
        assert "company" not in result.categories, result.reasons
    else:
        assert ticker in result.tickers, (text, result.reasons)


# --------------------------------------------------------------------------- precise vs broad keywords (SCOR-5)


@pytest.mark.parametrize(
    ("text", "category"),
    [
        ("Great results from our team", "earnings"),
        ("Next quarter we ship faster", "earnings"),
        ("Copilot in Outlook is now available", "guidance"),
        ("Weather forecast says rain", "guidance"),
        ("FSD v14.2 release notes are out", "offering"),
        ("A new offering for developers", "offering"),
        ("NASA space probe reaches Jupiter", "regulatory"),
    ],
)
def test_broad_keywords_flag_but_do_not_score(rules: FastRules, text: str, category: str) -> None:
    result = evaluate(rules, text, account=None)
    assert category in result.categories  # still a hint for Claude
    assert result.rule_score <= 2, (text, result)  # but adds nothing to the rules-only score


@pytest.mark.parametrize(
    ("text", "category"),
    [
        ("Q3 results are in", "earnings"),
        ("Record quarter for deliveries", "earnings"),
        ("We beat estimates", "earnings"),
        ("Raising our full-year outlook", "guidance"),
        ("Full-year forecast lowered", "guidance"),
        ("Priced $1B of convertible senior notes", "offering"),
        ("4.25% senior notes due 2030", "offering"),
        ("Launching a secondary offering", "offering"),
        ("FTC opens probe into the deal", "regulatory"),
        ("Antitrust probe into cloud pricing", "regulatory"),
    ],
)
def test_precise_keywords_score_as_strong(rules: FastRules, text: str, category: str) -> None:
    result = evaluate(rules, text, account=None)
    assert category in result.categories
    assert result.rule_score >= 2, (text, result)


def test_ray_ban_is_not_a_ban(rules: FastRules) -> None:
    assert "regulatory" not in evaluate(rules, "Ray-Ban glasses now shipping").categories
    assert "regulatory" in evaluate(rules, "New export-ban on chips").categories
    assert "regulatory" in evaluate(rules, "The EU banned the app").categories


def test_numbers_scoring_unchanged_by_precision_split(rules: FastRules) -> None:
    satya = evaluate(rules, "Great results from our Azure team this quarter, release notes here", account=None)
    assert satya.rule_score <= 2  # was 9 ("urgent") before broad words stopped counting as strong


# --------------------------------------------------------------------------- long input (SEC-1)


def _best_of(n: int, fn) -> float:
    import time

    best = float("inf")
    for _ in range(n):
        t0 = time.perf_counter()
        fn()
        best = min(best, time.perf_counter() - t0)
    return best


@pytest.mark.parametrize(
    "text",
    ["1," * 12_500, "1" * 25_000, "1." * 12_500, "1%" * 12_500, "₿1," * 8_333, "$1," * 8_333, " " * 25_000],
    ids=["1-comma", "digits", "1-dot", "1-percent", "btc-amount", "dollar", "spaces"],
)
def test_long_adversarial_text_evaluates_fast(rules: FastRules, text: str) -> None:
    quoted = ReferencedPost(type="quoted", id="2", text=text)
    own_post = make_post(text)
    quoting = make_post("lol", referenced=[quoted, ReferencedPost(type="replied_to", id="3", text=text)])
    # was 43 s for 25k chars of "1," (quadratic number patterns); now linear and the text is capped
    assert _best_of(3, lambda: rules.evaluate(own_post, ELON)) < 0.1
    assert _best_of(3, lambda: rules.evaluate(quoting, ELON)) < 0.1


def test_number_patterns_are_linear_even_uncapped() -> None:
    from ceoscan.scoring.rules import _BITCOIN_AMOUNT_RE, _NUMBER_RES

    for text in ("1," * 12_500, "1" * 25_000, "1.1," * 6_250):
        for rx in (*_NUMBER_RES, _BITCOIN_AMOUNT_RE):
            assert _best_of(2, lambda rx=rx, text=text: list(rx.finditer(text))) < 0.05, rx.pattern


def test_legit_numbers_still_match_after_redos_fix(rules: FastRules) -> None:
    for text, value in [
        ("500,000 vehicles", "500,000 vehicles"),
        ("bought 1,000 BTC today", "1,000 BTC"),
        ("1.2M units", "1.2M units"),
        ("21,000 ₿ held", None),
        ("grew.500 million users", "500 million users"),
    ]:
        result = evaluate(rules, text)
        assert result.has_numbers or "bitcoin" in result.categories, text
        if value:
            assert any(value in r for r in result.reasons), (text, result.reasons)
    assert "bitcoin" in evaluate(rules, "21,000 ₿ held").categories


def test_very_long_post_is_capped_but_start_is_scanned(rules: FastRules) -> None:
    from ceoscan.scoring.rules import MAX_POST_CHARS

    text = "Tesla raises full-year guidance. " + "x " * MAX_POST_CHARS + " $NVDA"
    result = evaluate(rules, text)
    assert "guidance" in result.categories
    assert "NVDA" not in result.tickers  # beyond the scanned prefix (Claude still sees all of it)


def test_detected_tickers_are_bounded(rules: FastRules) -> None:
    import itertools
    import string

    from ceoscan.scoring.rules import MAX_TICKERS

    tags = " ".join("$" + "".join(t) for t in itertools.islice(itertools.product(string.ascii_uppercase, repeat=3), 500))
    assert len(evaluate(rules, tags).tickers) == MAX_TICKERS
