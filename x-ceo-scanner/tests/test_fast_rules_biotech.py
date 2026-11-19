"""Fast rules for biotech binary catalysts (clinical readouts, FDA actions, licensing, offerings).

The motivating case is a small-cap biotech on the operator's own list (APMD = Apnimed, FDA
decision due 2027-02-28): before these rules no biotech catalyst reached the alert threshold in
rules-only mode (Claude down), because "met its primary endpoint" or "Breakthrough Therapy
designation" matched no strong category. Now one precise clinical / fda_action match on a post
naming the company scores 3 (company) + 2 (strong) + 1 (my ticker) + 1 (binary-catalyst bonus) =
7, capped at scoring.fallback_max_impact (7) = the alert threshold, while ordinary CEO chatter that
happens to share a word ("approval rating", "Fast track your career") stays quiet.

Rules-only scoring is not only an outage path (it is used whenever scoring is disabled or there is
no API key), so the second half of this file pins the false positives a review found against the
real watchlist: big-pharma label / safety-information posts, generic English ("I stopped dosing",
"free trial enrollment is paused", "topline data" = revenue), look-alike phrases ("priority review
for your mortgage application"), acronyms (a non-disclosure NDA, $CRL, the 50 EMA), weak brand
categories, device clearances from person accounts, and reminders / questions / recaps about APMD.
"""

from __future__ import annotations

import time
from datetime import UTC, datetime

import pytest

from ceoscan.config import ScoringSettings
from ceoscan.models import Account, Post, ReferencedPost, RuleResult, Watchlist
from ceoscan.scoring.claude import fallback_score
from ceoscan.scoring.rules import (
    _COMPILED_KEYWORDS,
    BINARY_CATALYST_CATEGORIES,
    CATEGORY_ORDER,
    MAX_POST_CHARS,
    STRONG_CATEGORIES,
    FastRules,
    own_text,
)

NOW = datetime(2026, 11, 19, 17, 0, tzinfo=UTC)
CAP = ScoringSettings().fallback_max_impact
THRESHOLD = ScoringSettings().threshold

APNIMED = Account(handle="apnimed", kind="company", tickers=("APMD",), user_id="1298383946892414983")
ELON = Account(handle="elonmusk", tickers=("TSLA", "SPCX"), user_id="44196397")


@pytest.fixture(scope="module")
def rules() -> FastRules:
    return FastRules(
        Watchlist(
            accounts=(APNIMED, ELON),
            keyword_rules=(),
            my_tickers=("APMD", "TSLA", "SPCX", "NVDA"),
            company_names={
                "APMD": ("Apnimed", "AD109", "Oxnimbi"),
                "TSLA": ("Tesla", "Cybertruck", "FSD"),
                "SPCX": ("SpaceX", "Starship"),
            },
        )
    )


def post(text: str, account: Account | None, refs: list[ReferencedPost] | None = None) -> Post:
    return Post(
        id="1990000000000000000",
        text=text,
        author_id=account.user_id if account and account.user_id else "999",
        created_at=NOW,
        received_at=NOW,
        source="replay",
        author_username=account.handle if account else "newsdesk",
        referenced=refs or [],
    )


def evaluate(rules: FastRules, text: str, account: Account | None = None) -> RuleResult:
    return rules.evaluate(post(text, account), account)


def impact(rules: FastRules, text: str, account: Account | None = None) -> int:
    return fallback_score(evaluate(rules, text, account), account, "timeout", max_impact=CAP).impact


# --------------------------------------------------------------------------- catalysts alert


# (text, author, category that must be detected). Most are news-desk posts (keyword rule, no
# watched account) naming Apnimed / AD109 / Oxnimbi; "acct" posts come from @apnimed itself.
CATALYSTS: list[tuple[str, str, str]] = [
    ("FDA accepts Apnimed NDA for AD109; PDUFA date set for February 28, 2027", "kw", "fda_action"),
    ("Apnimed receives FDA approval for Oxnimbi, the first oral pill for obstructive sleep apnea", "kw", "fda_action"),
    ("Today the FDA approved Oxnimbi (aroxybutynin and atomoxetine) for adults with OSA.", "acct", "fda_action"),
    ("FDA issues complete response letter (CRL) for Apnimed's AD109", "kw", "fda_action"),
    ("Apnimed's Phase 3 trial of AD109 met its primary endpoint", "kw", "clinical"),
    ("Apnimed announces positive topline results from Phase 3 LunAIRo trial of AD109", "kw", "clinical"),
    ("Apnimed's Phase 3 study of AD109 failed to meet its primary endpoint", "kw", "clinical"),
    ("FDA places clinical hold on Apnimed's sulthiame program", "kw", "clinical"),
    ("AD109 granted Breakthrough Therapy designation by FDA", "kw", "fda_action"),
    ("FDA grants priority review to Apnimed's NDA for AD109", "kw", "fda_action"),
    ("FDA schedules advisory committee meeting to review Apnimed's AD109", "kw", "fda_action"),
    ("FDA panel votes 9-4 in favor of Apnimed's sleep apnea pill $APMD", "kw", "fda_action"),
    ("Panel votes 9-4 in favor of Apnimed's sleep apnea pill", "kw", "fda_action"),
    ("Apnimed submits sNDA for label expansion of Oxnimbi to include mild OSA", "kw", "fda_action"),
    ("Apnimed signs licensing deal with Pfizer for AD109 ex-US rights for $150M upfront", "kw", "partnership"),
    (
        (
            "Apnimed enters exclusive license agreement with Shionogi for Oxnimbi in Japan; $50 million upfront and "
            "up to $400 million in milestones"
        ),
        "kw",
        "partnership",
    ),
    ("Apnimed prices $200 million underwritten offering of common stock at $18.00 per share", "kw", "offering"),
    ("FDA extends PDUFA goal date for Apnimed's AD109 by three months", "kw", "fda_action"),
    (
        "Apnimed: FDA extended the review of the Oxnimbi NDA after a major amendment; new action date May 28, 2027",
        "kw",
        "fda_action",
    ),
    ("$APMD lock-up expiration on January 27 could unlock 40 million shares", "kw", "offering"),
    ("FDA issues refuse-to-file letter for AD109", "kw", "fda_action"),
    ("Oxnimbi approved with boxed warning and REMS requirement", "kw", "fda_action"),
    ("Apnimed's AD109 granted Fast Track designation", "kw", "fda_action"),
    ("Independent DSMB recommends Apnimed stop the AD109 trial early for efficacy", "kw", "clinical"),
    ("Dosing in the AD109 Phase 2 trial has been paused, Apnimed says", "kw", "clinical"),
    ("FDA grants accelerated approval to Apnimed's AD109", "kw", "fda_action"),
    ("Apnimed's NDA accepted under priority review", "kw", "fda_action"),
    ("Positive CHMP opinion for Oxnimbi in the EU", "kw", "fda_action"),
    # the company's own account, no alias in the text: credited to its ticker
    ("Today the FDA approved our first medicine for obstructive sleep apnea", "acct", "fda_action"),
    ("We announced positive topline data from our Phase 3 program today", "acct", "clinical"),
    # press-release headline shapes, negative readouts, and the approval tweet with its safety footer
    (
        "Apnimed Announces Positive Topline Results from SynAIRgy Phase 3 Trial of AD109 in Obstructive Sleep Apnea",
        "kw",
        "clinical",
    ),
    ("Apnimed is pleased to announce positive topline results from the Phase 3 LunAIRo trial", "kw", "clinical"),
    ("Apnimed Announces U.S. FDA Approval of OXNIMBI", "kw", "fda_action"),
    ("US FDA approves Apnimed's sleep apnea pill", "kw", "fda_action"),
    ("Apnimed receives FDA Fast Track designation for AD109", "kw", "fda_action"),
    ("Apnimed's AD109 didn't meet its primary endpoint in the SynAIRgy trial", "kw", "clinical"),
    ("FDA rejects Apnimed's sleep apnea drug, issues CRL", "kw", "fda_action"),
    ("Apnimed gets a CRL for AD109", "kw", "fda_action"),
    ("FDA advisory committee votes 10-2 in favor of Apnimed's AD109", "kw", "fda_action"),
    ("As expected, Apnimed's Phase 3 trial met its primary endpoint", "kw", "clinical"),
    ("No surprise: the FDA approves Apnimed's Oxnimbi", "kw", "fda_action"),
    ("We're thrilled to share that the FDA has approved Oxnimbi", "acct", "fda_action"),
    (
        "Today the FDA approved Oxnimbi for adults with OSA. Please see full Prescribing Information, including "
        "Boxed Warning.",
        "acct",
        "fda_action",
    ),
]

WHO = {"kw": None, "acct": APNIMED}


@pytest.mark.parametrize(("text", "who", "category"), CATALYSTS, ids=[c[0][:40] for c in CATALYSTS])
def test_biotech_catalysts_reach_the_alert_threshold_rules_only(
    rules: FastRules, text: str, who: str, category: str
) -> None:
    account = WHO[who]
    r = evaluate(rules, text, account)
    assert category in r.categories, (text, r.categories, r.reasons)
    score = fallback_score(r, account, "timeout", max_impact=CAP)
    assert score.impact == CAP == THRESHOLD, (text, r)  # alerts (daytime "high"), never urgent / SMS
    assert score.tickers[0] == "APMD", (text, score.tickers)


def test_precise_catalyst_from_one_strong_category_is_exactly_seven(rules: FastRules) -> None:
    # 3 (company) + 2 (clinical) + 1 (APMD is mine) + 1 (binary catalyst) - no other category involved
    r = evaluate(rules, "Apnimed's Phase 3 trial of AD109 met its primary endpoint")
    assert r.categories == ["company", "clinical"]
    assert r.rule_score == r.own_rule_score == 7


def test_binary_catalyst_bonus_counts_once(rules: FastRules) -> None:
    # clinical and fda_action both precise: 3 + 2 + 2 + 1 (mine) + 1 bonus (not 2)
    r = evaluate(rules, "AD109 met its primary endpoint and received Breakthrough Therapy designation")
    assert {"clinical", "fda_action"} <= set(r.categories)
    assert r.rule_score == 9
    assert BINARY_CATALYST_CATEGORIES <= STRONG_CATEGORIES


def test_fda_token_of_a_binary_catalyst_does_not_also_count_as_regulatory(rules: FastRules) -> None:
    # "FDA approves X" is one event: 3 + 2 (fda_action) + 1 (mine) + 1 (bonus) = 7, not 9 with regulatory's +2
    r = evaluate(rules, "FDA approves Apnimed's Oxnimbi")
    assert {"regulatory", "fda_action"} <= set(r.categories)  # both labels still go to Claude
    assert r.rule_score == 7
    # a hedged / broad FDA mention keeps regulatory's +2 as before
    assert evaluate(rules, "Apnimed is waiting on FDA approval for AD109").rule_score == 6


def test_commercial_launch_is_precise_product_but_not_a_binary_catalyst(rules: FastRules) -> None:
    # +2 (precise product) instead of +1, no bonus: material, but scheduled after the approval
    for text, account in (
        ("Apnimed announces U.S. commercial launch of Oxnimbi", None),
        ("Oxnimbi is now available by prescription in the U.S.", APNIMED),
    ):
        r = evaluate(rules, text, account)
        assert "product" in r.categories, text
        assert impact(rules, text, account) == 6, (text, r)
    assert impact(rules, "Apnimed announces Oxnimbi", None) == 5  # broad "announces": +1 only


def test_offering_and_licensing_fixes(rules: FastRules) -> None:
    priced = evaluate(rules, "Apnimed prices $200 million underwritten offering of common stock")
    assert "offering" in priced.categories
    assert any("underwritten offering" in x for x in priced.reasons)
    # "prices" is a noun here: still only the broad "offering" hint (no strong score)
    noun = evaluate(rules, "Lower prices on our cloud offering this week")
    assert "offering" in noun.categories and noun.rule_score <= 2
    lockup = evaluate(rules, "The IPO lock-up period ends Tuesday for Apnimed insiders")
    assert "offering" in lockup.categories
    assert impact(rules, "The IPO lock-up period ends Tuesday for Apnimed insiders") == 6  # 3 + 2 + 1


# --------------------------------------------------------------------------- labels Claude sees


def test_clinical_results_are_not_labelled_earnings(rules: FastRules) -> None:
    for text in (
        "Apnimed announces positive topline results from Phase 3 LunAIRo trial of AD109",
        "Phase 3 results for AD109 are out",
        "Results from the pivotal trial of AD109 are in",
    ):
        r = evaluate(rules, text)
        assert "clinical" in r.categories, text
        assert "earnings" not in r.categories, (text, r.reasons)
    # real earnings keep their label
    assert "earnings" in evaluate(rules, "Apnimed Q3 results: revenue of $12 million").categories
    assert "earnings" in evaluate(rules, "Great results from our team").categories


def test_licensing_deals_are_partnerships_not_corporate_status(rules: FastRules) -> None:
    for text in (
        "Apnimed signs licensing deal with Pfizer for AD109 ex-US rights for $150M upfront",
        "Apnimed enters exclusive license agreement with Shionogi for Oxnimbi in Japan",
        "Apnimed was granted an exclusive license to the compound",
        "Upfront payment of $50 million and milestone payments of up to $400 million",
    ):
        r = evaluate(rules, text)
        assert "partnership" in r.categories, (text, r.reasons)
        assert "corporate_status" not in r.categories, (text, r.reasons)
    # a bank / exchange licence is still corporate status, and still scores as strong
    bank = evaluate(rules, "We were granted a banking license in the EU", ELON)
    assert "corporate_status" in bank.categories and "partnership" not in bank.categories
    assert bank.rule_score == 6  # account credit 3 + 2 + 1 (unchanged)
    # end-user licence agreements are not deals
    assert evaluate(rules, "Our end user license agreement has been updated").categories == []


def test_bare_trial_mention_only_flags(rules: FastRules) -> None:
    trial = evaluate(rules, "Apnimed is enrolling patients in a Phase 3 trial of AD109")
    plain = evaluate(rules, "Apnimed is enrolling patients for AD109")
    assert "clinical" in trial.categories  # a hint for Claude ...
    assert trial.rule_score == plain.rule_score == 4  # ... that adds nothing to the rules-only score
    for plan in ("Apnimed plans to file the NDA for AD109 next year", "Apnimed plans to submit the NDA in Q1"):
        assert evaluate(rules, plan).rule_score == 4, plan  # a plan is not an FDA action
    assert "fda_action" in evaluate(rules, "Apnimed submitted the NDA for AD109").categories
    waiting = evaluate(rules, "Apnimed is waiting on FDA approval for AD109")
    assert "fda_action" not in waiting.categories and waiting.rule_score == 6  # regulatory only, as before


def test_new_categories_are_inserted_after_regulatory_without_reordering() -> None:
    old = (
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
        "corporate_status",
        "incident",
        "buyback",
        "capital_return",
        "offering",
        "bitcoin",
        "numbers",
    )
    assert tuple(c for c in CATEGORY_ORDER if c in old) == old
    i = CATEGORY_ORDER.index("regulatory")
    assert CATEGORY_ORDER[i + 1 : i + 3] == ("clinical", "fda_action")
    assert {"clinical", "fda_action"} <= STRONG_CATEGORIES
    assert list(_COMPILED_KEYWORDS) == [c for c in CATEGORY_ORDER if c in _COMPILED_KEYWORDS]


# --------------------------------------------------------------------------- controls stay quiet


@pytest.mark.parametrize(
    ("text", "account", "max_impact"),
    [
        ("Great night of sleep! #SleepAwarenessWeek", APNIMED, 1),
        ("Join Apnimed at the Jefferies Healthcare Conference next week", None, 4),  # company + mine, as before
        ("Join us at the Jefferies Healthcare Conference next week", APNIMED, 1),
        ("My approval rating is through the roof lol", ELON, 3),
        ("Phase 2 of the Master Plan is almost done", ELON, 3),
        ("The results are in and we won the hackathon!", ELON, 3),
        ("Fast track your career with our new course", ELON, 3),
        ("We hit our primary goal for the quarter", ELON, 3),
        ("Upfront: I love this car", ELON, 3),
        ("Our panel discussion at SXSW starts at 3pm", ELON, 3),
        ("Our expert panel discussion at SXSW starts at 3pm", ELON, 3),
        ("NASA space probe reaches Jupiter", ELON, 3),
        ("Starship flight 12 was a success, the data looks encouraging", ELON, 4),  # alias + mine, as before
        ("Encouraging data from the Starship flight test", ELON, 4),
        ("This pipe is leaking", ELON, 3),
        ("The futility of arguing on the internet", ELON, 3),
        ("Run-DMC is the best", ELON, 3),
        ("Signed an NDA with a new supplier today", ELON, 3),
        ("Rems and dreams, what a night", ELON, 3),
        ("The Senate passed a major amendment to the bill", ELON, 3),
        ("Senate panel votes 12-10 to advance the bill", ELON, 3),
        ("The panel voted unanimously for the best booth", ELON, 3),
        ("Elon joins the White House advisory committee", ELON, 3),
        ("Our new FSD build shows a statistically significant drop in interventions", ELON, 4),
        ("This could be a breakthrough therapy for paralysis", ELON, 3),
        ("Fast-track status for your visa application", ELON, 3),
        ("Tesla priority review for Cybertruck orders", ELON, 4),
        ("Our record label expansion continues", ELON, 3),
        ("Private-label expansion is our biggest growth driver", ELON, 3),
        ("I refuse to file my taxes late", ELON, 3),
        ("The new action date for the rally is Saturday", ELON, 3),
        ("Texas granted accelerated approval of the Starbase permits", ELON, 3),
        ("We paused FSD beta enrollment for the holidays", ELON, 4),
        ("Phase 2 trials of FSD showed a big improvement", ELON, 4),
        ("Positive study results show FSD is safer than humans", ELON, 4),
        ("The Tesla FSD free trial was paused for the holidays", ELON, 4),
        ("Tesla Solar: no upfront payment, $0 down", ELON, 4),
        ("Huge milestone: 1 million Cybertrucks delivered", ELON, 5),  # alias + numbers + mine, as before
        ("We secured exclusive rights to stream the game", ELON, 3),
    ],
)
def test_controls_stay_quiet_or_low(rules: FastRules, text: str, account: Account | None, max_impact: int) -> None:
    assert impact(rules, text, account) <= max_impact, (text, evaluate(rules, text, account))


def test_person_account_trial_permission_is_not_an_fda_action(rules: FastRules) -> None:
    # permission to START a trial (IND-style) from a watched person: regulatory as before, no bonus
    r = evaluate(rules, "Neuralink received FDA approval for its first human clinical trial", ELON)
    assert "fda_action" not in r.categories
    assert impact(rules, "Neuralink received FDA approval for its first human clinical trial", ELON) == 6


def test_acronyms_are_case_sensitive(rules: FastRules) -> None:
    assert "fda_action" in evaluate(rules, "Apnimed gets a CRL").categories
    assert "fda_action" not in evaluate(rules, "Apnimed gets a crl").categories
    assert "clinical" in evaluate(rules, "The DSMB recommends continuing the AD109 trial").categories
    assert "clinical" in evaluate(rules, "The DMC recommended stopping the study").categories
    assert "clinical" not in evaluate(rules, "Apnimed at the DMC conference").categories
    assert "fda_action" in evaluate(rules, "Apnimed NDA for AD109 accepted").categories
    assert "fda_action" not in evaluate(rules, "Apnimed nda accepted by the vendor").categories


def test_quoted_catalyst_lifts_a_bare_reply_only_halfway(rules: FastRules) -> None:
    # the author's own words still drive the score: "Big if true" quoting an approval is not an alert
    quoted = ReferencedPost(type="quoted", id="2", text="FDA approves Apnimed's Oxnimbi for adults with OSA")
    r = rules.evaluate(post("Big if true", ELON, [quoted]), ELON)
    assert "fda_action" in r.categories and "fda_action" not in r.own_categories
    assert fallback_score(r, ELON, "timeout", max_impact=CAP).impact < THRESHOLD


# --------------------------------------------------------------------------- linear time (SEC-1)

HOSTILE_TOKENS = [
    "phase 3 ",
    "Phase 3 trial of ",
    "ph.",
    "Phase 1/2/3/",
    "I",
    "approved ",
    "approved for ",
    "FDA ",
    "met its ",
    "primary endpoint ",
    "positive topline ",
    "results from the ",
    "$1,",
    "$150M ",
    "up to $1 ",
    "lock-up ",
    "prices $1 ",
    "licensing ",
    "stop the trial ",
    "paused ",
    "statistically significant x ",
    "NDA ",
    "submits the ",
    "extended the review of the ",
    "FDA advisory ",
    "panel votes 1-",
    "expiration of the ",
    "receives FDA ",
    "dosing ",
    "upfront payment of ",
    "label ",
    "major amendment to the ",
    "-",
    " ",
    "'",
    "a ",
    # added with the review fixes: look-aheads, hedge windows, "'s NDA", deal amounts
    "positive phase 3 results ",
    "positive trial results ",
    "if FDA approves ",
    "FDA approved x for ",
    "Apnimed's NDA ",
    "a's ",
    "x'",
    "up to $1M in ",
    "results from the study ",
    "positive results from the phase 3 ",
    "has received breakthrough therapy designation ",
    "NDA for x ",
    "Fast Track designation ",
    "50 EMA ",
    "IPO lock up ",
    "Positive phase 3 results for x in a b c d e f g h i j k l m n o p q r s t u v w x y z ",
]


@pytest.mark.parametrize("token", HOSTILE_TOKENS)
def test_biotech_patterns_are_linear_on_hostile_text(rules: FastRules, token: str) -> None:
    text = (token * (25_000 // len(token) + 1))[:25_000]
    capped = text[:MAX_POST_CHARS]
    for cat in ("clinical", "fda_action", "partnership", "product", "offering", "corporate_status", "earnings"):
        kw = _COMPILED_KEYWORDS[cat]
        for rx in (*kw.broad, *kw.precise):
            fastest = float("inf")
            for _ in range(3):  # best of 3: one scheduler stall on a busy machine is not a regex problem
                t0 = time.perf_counter()
                list(rx.finditer(capped))
                fastest = min(fastest, time.perf_counter() - t0)
            assert fastest < 0.05, (cat, rx.pattern[:80], token)
    own = post(text, ELON)
    quoting = post(
        "lol",
        ELON,
        [ReferencedPost(type="quoted", id="2", text=text), ReferencedPost(type="replied_to", id="3", text=text)],
    )
    for p in (own, quoting):
        best = float("inf")
        for _ in range(3):
            t0 = time.perf_counter()
            rules.evaluate(p, ELON)
            best = min(best, time.perf_counter() - t0)
        assert best < 0.1, token


# =========================================================================== review regressions
#
# The accounts below mirror config/watchlist.yaml (handle, kind, tickers, aliases) so the scores
# are the ones the operator would get; the fixture is copied here so these tests do not change
# when the watchlist is edited.


def _acct(handle: str, kind: str, *tickers: str) -> Account:
    return Account(handle=handle, kind=kind, tickers=tickers)  # type: ignore[arg-type]  # kind is a Literal


AI_CAPEX = ("NVDA", "AMD", "SMCI", "MU", "MSFT", "CEG", "VST", "TLN")
AI_DEMAND = ("NVDA", "AMD", "SMCI", "MSFT")
WIDE = {
    a.handle: a
    for a in (
        ELON,
        APNIMED,
        _acct("sama", "person", *AI_DEMAND),
        _acct("saylor", "person", "MSTR", "CLSK"),
        _acct("brian_armstrong", "person", "COIN", "MSTR", "CLSK", "HOOD"),
        _acct("tim_cook", "person", "AAPL"),
        _acct("johnternus", "person", "AAPL"),
        _acct("satyanadella", "person", "MSFT", *AI_CAPEX),
        _acct("finkd", "person", "META", *AI_CAPEX),
        _acct("tobi", "person", "SHOP"),
        _acct("BillRMcDermott", "person", "NOW"),
        _acct("MichaelDell", "person", "DELL"),
        _acct("OpenAI", "company", *AI_DEMAND),
        _acct("nvidia", "company", "NVDA"),
        _acct("MicronTech", "company", "MU"),
        _acct("intel", "company", "INTC"),
        _acct("Supermicro", "company", "SMCI"),
        _acct("SoFi", "company", "SOFI"),
        _acct("RobinhoodApp", "company", "HOOD"),
        _acct("Visa", "company", "V"),
        _acct("IBKR", "company", "IBKR"),
        _acct("ConstellationEG", "company", "CEG"),
        _acct("VistraCorp", "company", "VST"),
        _acct("GEVernova", "company", "GEV"),
        _acct("Cisco", "company", "CSCO"),
        _acct("PalantirTech", "company", "PLTR"),
        _acct("Uber", "company", "UBER"),
        _acct("Airbnb", "company", "ABNB"),
        _acct("MonsterEnergy", "company", "MNST"),
        _acct("EliLillyandCo", "company", "LLY"),
        _acct("abbvie", "company", "ABBV"),
        _acct("Amgen", "company", "AMGN"),
        _acct("AstraZeneca", "company", "AZN"),
    )
}


@pytest.fixture(scope="module")
def wide() -> FastRules:
    accounts = tuple(WIDE.values())
    mine = ["APMD"] + [t for a in accounts for t in a.tickers]
    return FastRules(
        Watchlist(
            accounts=accounts,
            keyword_rules=(),
            my_tickers=tuple(dict.fromkeys(mine)),  # like load_watchlist: every account's tickers are "mine"
            company_names={
                "TSLA": ("Tesla", "Cybertruck", "Optimus", "Robotaxi", "Cybercab", "Model Y", "Model 3", "FSD"),
                "SPCX": ("SpaceX", "Starlink", "Starship", "Falcon 9"),
                "NVDA": ("Nvidia", "NVIDIA", "Blackwell", "Rubin", "GeForce", "CUDA", "GB200", "GB300"),
                "MSFT": ("Microsoft", "Azure", "Copilot"),
                "AAPL": ("Apple", "iPhone", "Vision Pro"),
                "META": ("Meta", "Instagram", "WhatsApp", "Llama"),
                "MSTR": ("MicroStrategy", "Strategy"),
                "HOOD": ("Robinhood",),
                "SOFI": ("SoFi",),
                "COIN": ("Coinbase",),
                "INTC": ("Intel", "18A", "14A", "Gaudi"),
                "MU": ("Micron", "HBM"),
                "CEG": ("Constellation Energy",),
                "VST": ("Vistra",),
                "TLN": ("Talen Energy", "Talen"),
                "APMD": ("Apnimed", "AD109", "Oxnimbi", "aroxybutynin", "LunAIRo", "SynAIRgy"),
                "CSCO": ("Cisco", "Splunk", "Webex", "Meraki"),
                "GEV": ("GE Vernova", "Vernova", "BWRX-300"),
                "PLTR": ("Palantir", "Maven Smart System"),
                "NOW": ("ServiceNow",),
                "SHOP": ("Shopify",),
                "UBER": ("=Uber", "Uber Eats"),
                "ABNB": ("Airbnb",),
                "LLY": ("Eli Lilly", "Mounjaro", "Zepbound", "orforglipron", "retatrutide", "Kisunla"),
                "AZN": ("AstraZeneca", "Enhertu", "Tagrisso", "Farxiga", "baxdrostat"),
                "ABBV": ("AbbVie", "Skyrizi", "Rinvoq", "Humira", "Botox"),
                "AMGN": ("Amgen", "Repatha", "MariTide"),
                "MNST": ("Monster Beverage", "Monster Energy"),
                "V": ("=Visa", "Visa Direct"),
                "IBKR": ("Interactive Brokers", "IBKR"),
            },
        )
    )


def wide_impact(rules: FastRules, handle: str | None, text: str) -> int:
    account = WIDE[handle] if handle else None
    return impact(rules, text, account)


def strong_biotech(rules: FastRules, text: str) -> set[str]:
    """clinical / fda_action categories with an unhedged precise match in ``text`` (author-independent)."""
    p = post(text, None)
    return rules._scan(own_text(p), p).strong & BINARY_CATALYST_CATEGORIES


def assert_quiet(rules: FastRules, handle: str | None, text: str) -> None:
    r = evaluate(rules, text, WIDE[handle] if handle else None)
    assert wide_impact(rules, handle, text) <= 6, (handle, text, r)
    assert not strong_biotech(rules, text), (handle, text, r.reasons)


# --------------------------------------------------------------------------- FP-1 big-pharma promo / ISI posts

PHARMA_PROMO = [
    ("EliLillyandCo", "Zepbound is approved for adults with obesity. See full Prescribing Information and Boxed "
     "Warning: lilly.com/zep"),
    ("EliLillyandCo", "Kisunla has a Boxed Warning for ARIA. Talk to your doctor. kisunla.lilly.com"),
    ("EliLillyandCo", "10 years ago today the FDA approved Trulicity. Thank you to the patients and teams who made "
     "it possible."),
    ("abbvie", "RINVOQ is approved for adults with moderate to severe ulcerative colitis. Please see full "
     "Prescribing Information, including BOXED WARNING."),
    ("abbvie", "SKYRIZI is approved for adult patients with moderate-to-severe plaque psoriasis."),
    ("abbvie", "BOTOX is FDA approved to prevent headaches in adults with chronic migraine."),
    ("Amgen", "Repatha is approved for adults with established cardiovascular disease to reduce the risk of heart "
     "attack."),
    ("Amgen", "Learn about the REMS program for our medicines at amgen.com/rems"),
    ("AstraZeneca", "ENHERTU is approved in Japan for patients with HER2-low metastatic breast cancer."),
    ("AstraZeneca", "Learn more about FARXIGA, approved for adults with chronic kidney disease. Important Safety "
     "Information: azn.com/farxiga"),
    ("EliLillyandCo", "In 2012 the FDA approved Kalydeco in record time. Great case study."),
]


@pytest.mark.parametrize(("handle", "text"), PHARMA_PROMO, ids=[t[:40] for _, t in PHARMA_PROMO])
def test_pharma_label_and_safety_posts_do_not_alert(wide: FastRules, handle: str, text: str) -> None:
    assert_quiet(wide, handle, text)


def test_pharma_accounts_still_alert_on_a_real_approval_or_readout(wide: FastRules) -> None:
    for handle, text in (
        ("EliLillyandCo", "Today the FDA approved Kisunla for early Alzheimer's disease. See full Prescribing "
         "Information and Boxed Warning."),
        ("EliLillyandCo", "Orforglipron met the primary endpoint in ACHIEVE-1, with A1C reduction of 1.6%"),
        ("AstraZeneca", "We have discontinued the Phase 3 trial of baxdrostat"),
        (None, "AstraZeneca receives complete response letter for baxdrostat"),
    ):
        assert wide_impact(wide, handle, text) == THRESHOLD, (handle, text)


# --------------------------------------------------------------------------- FP-2 clinical words in everyday English

GENERIC_CLINICAL = [
    ("elonmusk", "I stopped dosing ketamine years ago. The media story is false."),
    ("elonmusk", "FSD free trial enrollment is paused for a few days while we upgrade the servers"),
    ("elonmusk", "Starlink free trial enrollment has been suspended in Brazil"),
    ("elonmusk", "Positive trial results for Robotaxi in Austin, expanding to 10 more cities"),
    ("elonmusk", "Encouraging Phase 2 results from the Optimus factory deployment"),
    ("sama", "Robust Phase 1 results at Stargate Abilene; Phase 2 comes online next month"),
    ("sama", "topline data from the gpt-6 eval is in and it is very good"),
    ("sama", "Study enrollment paused for the summer; we resume in September"),
    ("Uber", "Encouraging trial results from our autonomous delivery pilot in Miami"),
    ("nvidia", "Positive trial results: NVIDIA Clara helps radiologists read scans 30% faster"),
    ("tobi", "BFCM topline data: $14.6B in sales, up 24%"),
    ("BillRMcDermott", "Robust topline results again. Our team delivers."),
    ("GEVernova", "Positive Phase 1 results from site prep at the first BWRX-300 in Ontario"),
    ("Supermicro", "Positive results from our late-stage liquid cooling validation"),
    (None, "Encouraging pivotal data from NVIDIA's Rubin bring-up, says analyst"),
    ("OpenAI", "Encouraging results from our study of ChatGPT Health with 500 patients"),
    (None, "Tesla Optimus dosing halted? Fake news."),
]


@pytest.mark.parametrize(("handle", "text"), GENERIC_CLINICAL, ids=[t[:40] for _, t in GENERIC_CLINICAL])
def test_generic_english_is_not_a_clinical_readout(wide: FastRules, handle: str | None, text: str) -> None:
    assert_quiet(wide, handle, text)


def test_topline_revenue_posts_keep_their_earnings_hint(wide: FastRules) -> None:
    # "topline" is revenue in finance: "robust topline results" is an earnings call, not a readout
    r = evaluate(wide, "Robust topline results again. Our team delivers.", WIDE["BillRMcDermott"])
    assert "earnings" in r.categories


# --------------------------------------------------------------------------- FP-3 look-alike FDA phrases

GENERIC_FDA = [
    ("SoFi", "SoFi Plus members get priority review for your mortgage application. Apply today."),
    ("RobinhoodApp", "Gold members: your Robinhood Gold Card application is now under priority review"),
    ("Visa", "Disputes from Visa Signature cardholders get priority review status"),
    ("IBKR", "IBKR grants priority review to Pro clients' margin requests"),
    ("elonmusk", "Fast-track designation for H-1B renewals would help a lot of engineers"),
    ("ConstellationEG", "Our Crane Clean Energy Center restart has been granted fast-track designation by the state"),
    ("ConstellationEG", "Constellation filed a major amendment to the license application for our Calvert Cliffs "
     "uprate"),
    ("VistraCorp", "FERC extended the review of the Vistra interconnection application by 60 days"),
    ("GEVernova", "BWRX-300 moves forward on the NRC's accelerated approval pathway for advanced reactors"),
    ("sama", "Texas has an accelerated approval program for data centers. Stargate benefits."),
    ("sama", "Honored that the White House AI advisory committee recommends open models"),
    ("brian_armstrong", "The CFTC Global Markets Advisory Committee voted to advance tokenized collateral. Big step."),
    ("elonmusk", "The appeals court panel voted 2-1 in favor of Tesla. Justice prevails."),
    ("elonmusk", "We pushed back the goal date for Optimus V3 production to next summer"),
    ("satyanadella", "We're extending the review period for Copilot feedback by 2 weeks"),
    ("tim_cook", "We are extending the review timeline for App Store submissions during the holidays"),
    ("MonsterEnergy", "Monster Energy Records label expansion into Latin America"),
    ("MonsterEnergy", "Our Reign label extension for 2027 adds 4 new flavors"),
    ("Uber", "Uber Teen accounts now approved for adults 18+ to supervise"),
    ("Uber", "Uber Health rides are now approved for patients in 20 more states"),
    ("Airbnb", "Experiences approved for children with parents in 30 cities"),
    ("elonmusk", "Social media should come with a black box warning lol"),
    ("elonmusk", "Neuralink approved for patients with ALS in Canada"),
]


@pytest.mark.parametrize(("handle", "text"), GENERIC_FDA, ids=[t[:40] for _, t in GENERIC_FDA])
def test_look_alike_phrases_are_not_fda_actions(wide: FastRules, handle: str, text: str) -> None:
    assert_quiet(wide, handle, text)


# --------------------------------------------------------------------------- FP-4 acronyms

ACRONYMS = [
    ("elonmusk", "NDA was approved by legal, send over the deck"),
    ("tobi", "They accepted our NDA in 10 minutes. Founders move fast."),
    ("Cisco", "Security advisory: update your CRL distribution points before Jan 1"),
    (None, "$CRL beats Q3 estimates, $NVDA flat"),
    (None, "$MSTR: the 50 ema rejected price again, watch $300"),
    ("saylor", "The 200-day EMA rejected bitcoin twice this month. Doesn't matter. Buy and hold."),
    (None, "$MSTR: the 50 EMA rejected price again"),
]


@pytest.mark.parametrize(("handle", "text"), ACRONYMS, ids=[t[:40] for _, t in ACRONYMS])
def test_everyday_acronyms_are_not_drug_applications_or_regulators(
    wide: FastRules, handle: str | None, text: str
) -> None:
    assert_quiet(wide, handle, text)
    assert "fda_action" not in evaluate(wide, text, WIDE[handle] if handle else None).categories


def test_drug_acronyms_with_drug_context_still_count(wide: FastRules) -> None:
    for text in (
        "Apnimed NDA for AD109 accepted",
        "Apnimed's NDA accepted under priority review",
        "Apnimed submitted the NDA for AD109",
        "FDA accepts Apnimed NDA for AD109",
        "Apnimed gets a CRL for AD109",
        "Apnimed receives CRL from the FDA",
        "EMA has accepted the marketing authorization application for Oxnimbi",
    ):
        assert strong_biotech(wide, text) == {"fda_action"}, text


# --------------------------------------------------------------------------- FP-5 weak categories stay weak

WEAK_PRECISE = [
    ("elonmusk", "Falcon 9 launches 29 Starlink satellites, SpaceX's 140th commercial launch this year"),
    ("elonmusk", "Tesla Semi commercial launch next month: 500 mile range, $150k"),
    ("nvidia", "DGX Spark commercial availability starts today at $3,999"),
    ("MicronTech", "HBM4 commercial launch with 1,000 customers sampling"),
    ("elonmusk", "Starlink now available in India with an upfront payment of $299 for the kit"),
    ("saylor", "Bitcoin doesn't need an upfront payment of $1,000 to get started. Buy $10 today."),
    ("MichaelDell", "Hit some huge milestone payments to our suppliers: $1 billion paid early"),
    ("Cisco", "Our new Enterprise License Agreement saves customers 30% on Webex"),
    ("satyanadella", "Our new Enterprise license agreement makes Copilot available to 1 million more seats"),
    ("finkd", "Llama 5 ships under a new license agreement that lets 99% of companies use it free"),
    ("MicronTech", "HBM4 commercial launch: 2.0 TB/s per stack"),
    ("intel", "Intel 18A commercial availability is on track for 2026"),
    ("PalantirTech", "Army milestone payments on Maven Smart System are on schedule"),
    ("EliLillyandCo", "Zepbound vials are available by prescription through LillyDirect starting at $349/month"),
]


@pytest.mark.parametrize(("handle", "text"), WEAK_PRECISE, ids=[t[:40] for _, t in WEAK_PRECISE])
def test_launch_and_licensing_brand_posts_stay_below_the_threshold(wide: FastRules, handle: str, text: str) -> None:
    assert wide_impact(wide, handle, text) <= 6, (handle, text, evaluate(wide, text, WIDE[handle]))


def test_a_precise_weak_category_is_not_news_about_the_accounts_own_company(wide: FastRules) -> None:
    # no alias, no ticker: a deal-size amount alone does not credit the account's tickers (no +3 / +1)
    r = evaluate(wide, "We signed a licensing deal with Pfizer: $150M upfront", WIDE["nvidia"])
    assert "partnership" in r.categories and r.tickers == []
    assert r.rule_score == 3  # partnership +2, numbers +1


# --------------------------------------------------------------------------- FP-6 device clearances, person accounts

DEVICE_AND_PERSON = [
    ("tim_cook", "Hypertension notifications on Apple Watch are now FDA cleared. Proud of our health team."),
    ("tim_cook", "The FDA has authorized our hearing aid feature on AirPods Pro. Life changing for millions."),
    ("johnternus", "Apple Watch sleep apnea notifications received FDA authorization in 150 countries"),
    ("elonmusk", "FDA approved Neuralink's Blindsight for its first human implant. Exciting times!"),
    ("elonmusk", "FDA grants Breakthrough Device designation to Neuralink Blindsight"),
]


@pytest.mark.parametrize(("handle", "text"), DEVICE_AND_PERSON, ids=[t[:40] for _, t in DEVICE_AND_PERSON])
def test_device_clearances_and_trial_permissions_stay_regulatory(wide: FastRules, handle: str, text: str) -> None:
    r = evaluate(wide, text, WIDE[handle])
    assert "regulatory" in r.categories
    assert_quiet(wide, handle, text)


def test_person_accounts_get_no_binary_catalyst_bonus(wide: FastRules) -> None:
    # a precise FDA event from a person account: 3 (own news) + 2 + 1 (own ticker) = 6, no +1 bonus
    r = evaluate(wide, "FDA approves Neuralink Blindsight for blind patients", ELON)
    assert "fda_action" in r.own_categories
    assert r.own_rule_score == 6
    # ... while the company's own account gets it (no alias needed)
    assert evaluate(wide, "Today the FDA approved our first medicine", APNIMED).own_rule_score == 7


# --------------------------------------------------------------------------- FP-7 APMD reminders, questions, recaps

APMD_NOT_NEWS = [
    "$APMD PDUFA is Feb 28, 2027. 101 days to go.",
    "Biotech catalysts to watch in Q1: Apnimed (PDUFA Feb 28), plus 6 others",
    "Apnimed CEO says the company remains on track for its Feb 28 PDUFA date",
    "Apnimed expects topline data from the SynAIRgy extension study in mid-2027",
    "Apnimed to present at the Jefferies Healthcare Conference. AD109 has received Fast Track designation from the "
    "FDA.",
    "Recall: Apnimed's LunAIRo Phase 3 met its primary endpoint back in 2025. Now all eyes on the FDA.",
    "What happens to $APMD if the FDA issues a CRL? A thread",
    "Will Oxnimbi need a boxed warning? atomoxetine has one. Thread on $APMD",
    "No FDA advisory committee is planned for Apnimed's AD109, the company says",
    "Apnimed completes enrollment in Phase 3 SynAIRgy trial; topline data expected in 2026",
    "Apnimed's NDA submission came in January; the FDA decision is due in February",
    "Apnimed expects topline results from the Phase 3 trial in Q2 2026",
    "Apnimed to announce topline results from Phase 3 SynAIRgy trial on Monday",
    "Topline results from the Phase 3 SynAIRgy trial are expected in Q2",
    "Has AD109 met its primary endpoint? We will know in Q2",
    "Apnimed's AD109 could win FDA approval in February",
    "Apnimed hasn't received a CRL for AD109",
    "In 2025 Apnimed's Phase 3 trial met its primary endpoint",
    "What is a complete response letter? A thread for new biotech investors",
]


@pytest.mark.parametrize("text", APMD_NOT_NEWS, ids=[t[:40] for t in APMD_NOT_NEWS])
def test_apmd_reminders_questions_and_recaps_do_not_alert(wide: FastRules, text: str) -> None:
    assert_quiet(wide, None, text)
    # ... but they still reach Claude as a hint
    assert {"clinical", "fda_action"} & set(evaluate(wide, text).categories), text


# --------------------------------------------------------------------------- REG-8 side effects on old categories


def test_clinical_words_do_not_hide_a_real_earnings_result(wide: FastRules) -> None:
    for text in (
        "Danaher-style beat: AbbVie Q3 results in clinical immunology up 23%, Skyrizi and Rinvoq lead",
        "Palantir Q3 results of the AIP study: revenue +96%",
        "AbbVie Q3 results beat: revenue up 10%, Skyrizi and Rinvoq up 23%",
    ):
        r = evaluate(wide, text)
        assert "earnings" in r.categories and any("q3 results" in x for x in r.reasons), (text, r.reasons)
        assert wide_impact(wide, None, text) == 7, text


def test_operating_licence_and_spaced_lock_up_keep_their_old_meaning(wide: FastRules) -> None:
    coin = evaluate(wide, "Coinbase secures an exclusive license to operate in Singapore")
    assert "corporate_status" in coin.categories and "partnership" not in coin.categories
    assert wide_impact(wide, None, "Coinbase secures an exclusive license to operate in Singapore") == 4
    yard = evaluate(wide, "Talen Energy workers: lock up ends at 6pm, please clear the yard")
    assert "offering" not in yard.categories
    for text in ("Apnimed IPO lock-up expires January 27", "The IPO lock up period ends Tuesday for Apnimed insiders"):
        assert "offering" in evaluate(wide, text).categories, text


# --------------------------------------------------------------------------- PERF-1


def test_partnership_precise_list_fails_fast_on_non_letter_runs() -> None:
    rx = _COMPILED_KEYWORDS["partnership"].precise[0]
    assert rx.pattern.startswith("(?=[A-Za-z$])")
    text = " " * MAX_POST_CHARS
    best = float("inf")
    for _ in range(3):
        t0 = time.perf_counter()
        assert rx.search(text) is None
        best = min(best, time.perf_counter() - t0)
    assert best < 0.005
