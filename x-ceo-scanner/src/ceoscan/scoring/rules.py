"""Stage 1 scoring: fast, deterministic regex rules.

`FastRules.evaluate` decides in microseconds whether a post looks market relevant. It is
deliberately recall-oriented (a false positive only costs one cheap Claude call), but it must
stay quiet on plain chit-chat ("Good morning!", "lol so true", "100%").

Two jobs, kept apart on purpose:

* **Flagging** (``RuleResult.flagged`` / ``categories``) uses broad keyword patterns. It gates
  keyword-rule posts and gives Claude hints, so recall matters more than precision.
* **The rules-only score** (``rule_score`` / ``own_rule_score``) is what an alert is built from
  when Claude is unavailable. A strong category counts there only when a *precise* pattern
  matched ("raised guidance", "convertible notes", "Q3 results"), not a broad word on its own
  ("outlook", "release notes", "amazing results").

Biotech binary catalysts get their own strong categories: ``clinical`` (trial readouts: "met its
primary endpoint", "positive topline results from the Phase 3 trial", clinical holds, halted
trials) and ``fda_action`` (FDA / EMA events: approvals, complete response letters, a PDUFA date
set or extended, advisory-committee votes, newly granted Breakthrough Therapy / Fast Track
designations, label expansions). Their precise patterns are event-shaped: a label statement ("X
is approved for adults with ..."), safety boilerplate ("has a Boxed Warning"), a reminder ("PDUFA
is Feb 28") or generic English ("I stopped dosing", "free trial enrollment is paused") only flags.
A precise match that is hedged - a question, a condition, a plan or a recap ("if the FDA issues a
CRL?", "expects topline results ...", "met its primary endpoint back in 2025") - only flags too.
A precise match adds a +1 "binary catalyst" bonus (once) for a news post or a company account,
and the FDA token of the same event does not count again as regulatory news, so a post naming a
watched company with one such match reaches the alert threshold (7) in rules-only mode, while a
person account posting an FDA headline (a device clearance, Neuralink) stays at 6.

The rules look at the post text plus the text of any quoted / replied-to / reposted post,
because "Yes" in reply to "Will Tesla raise guidance?" is market relevant. The post's OWN text
is also evaluated separately (``own_*`` fields) so the rules-only score is driven by what the
author wrote, not by the post they replied to or quoted.

Every pattern is written to scan in linear time (no match can start in the middle of a number,
bounded gaps only), and the scanned text is capped, so a crafted 25,000-character post cannot
stall the event loop.
"""

from __future__ import annotations

import itertools
import logging
import re
from dataclasses import dataclass, field
from re import Pattern

from ..models import Account, Post, RuleResult, Watchlist

logger = logging.getLogger(__name__)

# Category names (part of the contract: stored in the DB and shown in replay output).
CAT_TICKER = "ticker"
CAT_COMPANY = "company"
CAT_EARNINGS = "earnings"
CAT_GUIDANCE = "guidance"
CAT_M_AND_A = "m_and_a"  # includes take-private / "funding secured"
CAT_EXEC_CHANGE = "exec_change"  # CEO / CFO / chair departures and appointments
CAT_PARTNERSHIP = "partnership"
CAT_LAYOFFS = "layoffs"
CAT_PRODUCT = "product"
CAT_REGULATORY = "regulatory"
CAT_CLINICAL = "clinical"  # trial readouts: endpoints met / missed, topline data, clinical holds, halted trials
CAT_FDA_ACTION = "fda_action"  # FDA / EMA drug decisions: approvals, CRLs, PDUFA dates, designations, AdComs
CAT_CORPORATE_STATUS = "corporate_status"  # bankruptcy, delisting, bank charter, licenses
CAT_INCIDENT = "incident"  # outage, hack, breach, recall, halted / paused production
CAT_BUYBACK = "buyback"
CAT_CAPITAL_RETURN = "capital_return"  # dividends, stock splits
CAT_OFFERING = "offering"
CAT_BITCOIN = "bitcoin"  # bitcoin purchases AND sales
CAT_NUMBERS = "numbers"

CATEGORY_ORDER: tuple[str, ...] = (
    CAT_TICKER,
    CAT_COMPANY,
    CAT_EARNINGS,
    CAT_GUIDANCE,
    CAT_M_AND_A,
    CAT_EXEC_CHANGE,
    CAT_PARTNERSHIP,
    CAT_LAYOFFS,
    CAT_PRODUCT,
    CAT_REGULATORY,
    CAT_CLINICAL,
    CAT_FDA_ACTION,
    CAT_CORPORATE_STATUS,
    CAT_INCIDENT,
    CAT_BUYBACK,
    CAT_CAPITAL_RETURN,
    CAT_OFFERING,
    CAT_BITCOIN,
    CAT_NUMBERS,
)

# Categories that on their own usually mean "material company news" (+2 each in the rules-only
# score, but only when a precise pattern for the category matched).
STRONG_CATEGORIES: frozenset[str] = frozenset(
    {
        CAT_EARNINGS,
        CAT_GUIDANCE,
        CAT_M_AND_A,
        CAT_EXEC_CHANGE,
        CAT_LAYOFFS,
        CAT_REGULATORY,
        CAT_CLINICAL,
        CAT_FDA_ACTION,
        CAT_CORPORATE_STATUS,
        CAT_INCIDENT,
        CAT_BUYBACK,
        CAT_CAPITAL_RETURN,
        CAT_OFFERING,
        CAT_BITCOIN,
    }
)
# Categories that are often marketing / brand noise: worth a look, weaker signal (+1). A precise
# match (a deal-size amount: "$150M upfront"; a prescription-drug launch) counts +2, but never
# makes a watched account's post count as news about its own company (see `account_hit`).
WEAK_CATEGORIES: frozenset[str] = frozenset({CAT_PARTNERSHIP, CAT_PRODUCT})
#: A precise (unhedged) match in one of these is a binary biotech catalyst (trial readout, FDA
#: decision): it adds BINARY_CATALYST_BONUS once on top of its +2 for a news post or a company
#: account, so "<watched company> met its primary endpoint" scores 3 (company) + 2 + 1 (mine) + 1
#: = 7, the alert threshold, in rules-only mode. A person account gets no bonus: its FDA headlines
#: are mostly about devices or side projects ("Apple Watch is now FDA cleared", Neuralink).
BINARY_CATALYST_CATEGORIES: frozenset[str] = frozenset({CAT_CLINICAL, CAT_FDA_ACTION})
BINARY_CATALYST_BONUS = 1

#: Only this much text is scanned: the post itself, and each quoted / parent / reposted post.
#: (X posts can be 25,000 characters; Claude still sees the full text.)
MAX_POST_CHARS = 8_000
MAX_REFERENCED_CHARS = 4_000
MAX_REFERENCED_POSTS = 4
#: at most this many detected tickers are kept (a post listing 1,000 cashtags is spam)
MAX_TICKERS = 25

_TICKER_SHAPE = re.compile(r"^[A-Z][A-Z0-9.]{0,9}$")

# Tickers that are also everyday English words. In all-caps posts ("DOWNLOAD THE APP", "SHOP NOW",
# "MEME COIN", "IT IS NOW THE BIGGEST...") a bare uppercase match is almost never the stock, so these
# only count as $CASHTAGS (like 1-2 letter tickers). Company-name aliases still work as usual.
COMMON_WORD_TICKERS: frozenset[str] = frozenset(
    {
        "ALL", "AND", "ANY", "APP", "ARE", "BIG", "CAN", "CAR", "CASH", "CAT", "COIN", "DOW", "EAT", "EYE",
        "FAST", "FIVE", "FOR", "FUN", "GOOD", "HAS", "HOOD", "HOME", "HOT", "JOB", "KEY", "LIFE", "LOVE",
        "LOW", "MAIN", "META", "NEW", "NICE", "NOW", "ONE", "OPEN", "OUT", "PLAY", "REAL", "RUN", "SAFE",
        "SEE", "SHOP", "SNOW", "TECH", "TRUE", "TWO", "WELL", "WIN", "YOU",
    }
)

# $TSLA, $tsla, $BRK.B ; never "$5B" (must start with a letter). "$TSLA." -> TSLA.
_CASHTAG_RE = re.compile(r"(?<![A-Za-z0-9_$])\$([A-Za-z]{1,6}(?:\.[A-Za-z]{1,2})?)(?![A-Za-z0-9_])")

# Word boundary that also treats digits/underscore as word characters but works for
# aliases that themselves start/end with digits ("18A", "GB200", "Falcon 9").
_LB = r"(?<![A-Za-z0-9_])"
_RB = r"(?![A-Za-z0-9_])"
# Left boundary for number patterns: additionally, a match may not start right after "1," or
# "1." - i.e. in the middle of a number. Without this, every comma in "1,1,1,...,1" starts a new
# match attempt that scans to the end of the run (quadratic: 25k chars took 40+ s).
# "grew.500 million users" (no digit before the dot) still matches.
_NUM_LB = r"(?<![A-Za-z0-9_])(?<![0-9][,.])"


def _words(*alternatives: str) -> str:
    return _LB + "(?:" + "|".join(alternatives) + ")" + _RB


def _lead_words(first: str, *alternatives: str) -> str:
    """`_words` for long alternation lists whose every alternative starts with a character in ``first``.

    Same matches; the leading lookahead only makes every other position (spaces, digits,
    punctuation) fail at once instead of trying each alternative there.
    """
    return r"(?=" + first + r")" + _words(*alternatives)


def _alpha_words(*alternatives: str) -> str:
    """`_lead_words` for alternation lists whose every alternative starts with a letter."""
    return _lead_words(r"[A-Za-z]", *alternatives)


# --------------------------------------------------------------------------- keyword categories

# For each category: (broad case-insensitive, broad case-sensitive, precise case-insensitive,
# precise case-sensitive). Any match flags the category; only a precise match makes it count
# as STRONG in the rules-only score. Categories with no precise patterns treat every match as
# precise. Acronyms are case-sensitive so "atm" (at the moment) or "sec" (second) stay chit-chat.
_KwSpec = tuple[str | None, str | None, str | None, str | None]

# ("president" is left out on purpose: "the next President" is politics, not a company change)
_ROLE = r"(?:CEO|CFO|COO|CTO|chief\s+[a-z]+\s+officer|chair(?:man|woman|person)?)"
_PRIVATE_NOT_NOUN = (
    r"(?!\s+(?:jets?|planes?|equity|keys?|messages?|life|lives|data|accounts?|beta|island|school|"
    r"schools|sector|credit|label|road|room|party|sale|investors?|placement|company|companies|"
    r"time|event|events|tour|tours|chat|chats|dinner|meeting|meetings)\b)"
)
_LICENSE = r"licen[cs]es?"
# A licence in the corporate-status sense (bank / exchange / operating licence), not a drug or
# technology licensing deal ("an exclusive license", "license agreement"): those are partnerships.
# ("an exclusive license to operate in Singapore" is still an operating licence.)
_STATUS_LICENSE = (
    r"(?:(?<!exclusive\s)(?<!global\s)(?<!worldwide\s)|(?=licen[cs]es?\s+to\s+operate))" + _LICENSE
    + r"(?!\s+(?:agreements?|deals?|pacts?)\b)"
)
# "$150M", "$1.2 billion" inside a longer phrase (the match can only start at the "$")
_DEAL_AMOUNT = r"\$\s?\d[\d,.]*\s?(?:[MB]|mn|bn|million|billion)"

# --- biotech building blocks (all bounded; no quantifier nests over overlapping classes)
# "Phase 3", "phase III", "Ph. 2b", "Phase 1/2a", "Phase IIb/III"
_PHASE = r"(?:phase|ph\.?)\s?(?:[1-4][ab]?(?:/[1-4][ab]?)?|(?:IV|I{1,3})[ab]?(?:/(?:IV|I{1,3})[ab]?)?)"
# "primary endpoint", "co-primary endpoints", "primary and key secondary endpoints"
_ENDPOINT = (
    r"(?:(?:co-?)?primary|key\s+secondary|secondary)\s+(?:and\s+(?:key\s+)?secondary\s+)?(?:efficacy\s+)?end-?points?"
)
# ("robust" is left out: "robust topline results" is an earnings call - topline means revenue in finance)
_OUTCOME = r"(?:positive|negative|mixed|disappointing|encouraging|favou?rable)"
_OUTCOME_TOPLINE = r"(?:positive|negative|disappointing|encouraging|favou?rable)"  # not "mixed topline results"
_TRIAL_STOPPED = r"(?:halted|paused|suspended|stopped|discontinued|terminated)"
# a trial or study, but not a "free trial" (FSD, Starlink and app trials)
_TRIAL = r"(?<!free\s)(?<!free-)(?:trials?|stud(?:y|ies))"
# Words that make a generic phrase clinical. They are only looked for AHEAD of a match, inside the same sentence
# and at most 80 characters away: "positive Phase 2 results for AD109 in patients with OSA" counts, "Encouraging
# Phase 2 results from the Optimus factory deployment" and "Positive trial results for Robotaxi" do not.
_CLINICAL_WORD = (
    r"(?:patients?|participants|subjects|placebo|end-?points?|efficacy|tolerab(?:le|ility)|adverse|drugs?"
    r"|therap(?:y|ies|eutic)|treatments?|doses?|dosing|cohorts?|diseases?|clinical|randomi[sz]ed|double-blind"
    r"|statistical(?:ly)?)"
)
_CLINICAL_AHEAD = r"(?=[^.!?\n]{0,80}?" + _LB + _CLINICAL_WORD + _RB + r")"
# Regulators are matched case-sensitively ("the 50 ema rejected price again" is a moving average), and EMA never
# right after a number or a period word ("the 200-day EMA rejected bitcoin").
_EMA = r"(?<![0-9]\s)(?<!day\s)(?<!period\s)(?<!week\s)EMA"
_REGULATOR_CS = r"(?:FDA|" + _EMA + r"|MHRA|PMDA|NMPA|Health\s+Canada)"
_DRUG_APP = r"(?:new\s+drug\s+application|biologics\s+license\s+application|marketing\s+authori[sz]ation\s+application)"
# Acronyms stay case-sensitive, and "NDA" is also a non-disclosure agreement in upper case ("NDA was approved by
# legal", "they accepted our NDA"): an NDA only counts with drug context (see the fda_action patterns).
_DRUG_APP_CS = r"(?:NDA|BLA|sNDA|sBLA|MAA)"
_DRUG_APP_CS_NO_NDA = r"(?:BLA|sNDA|sBLA|MAA)"
# a verb, not a noun: "Apnimed's NDA accepted" is news, "Apnimed's NDA submission came in January" a recap
_APP_EVENT = r"(?:accepted|approved|submitted|filed|resubmitted|under\s+review)"
# Permission to START a trial is not a drug decision: "FDA approved Neuralink's Blindsight for its first human
# implant", "receives FDA approval for its first human clinical trial", "FDA approves human trials".
_TRIAL_PERMISSION = (
    r"(?:(?:\s+[\w'’-]+){0,4}?\s+(?:for|to\s+(?:begin|start|launch|initiate|run|conduct))\s+"
    r"(?:(?:its|a|an|the|our|their)\s+)?(?:(?:first[-\s]in[-\s]human|first\s+human|human|first)\s+)?"
    r"(?:clinical\s+)?(?:trials?|stud(?:y|ies)|implants?|testing)"
    r"|(?:\s+[\w'’-]+)?\s+(?:first[-\s]in[-\s]human|human|clinical)\s+(?:trials?|stud(?:y|ies)|testing))"
)
# "the FDA approved label", "FDA approved treatment for migraine": an adjective, not a decision
_NOT_ADJECTIVE = (
    r"(?!\s+(?:labels?|labell?ing|doses?|dosing|indications?|drugs?|treatments?|medicines?|medications?"
    r"|therap(?:y|ies)|products?|devices?|uses?|versions?|formulations?|generics?|options?|biosimilars?"
    r"|vaccines?|pills?)(?![A-Za-z0-9_]))"
)
# Expedited-program names that exist only for drugs. ("Fast Track" is handled apart: "fast-track designation
# by the state" is a permit; "Breakthrough Device" is a device pathway and stays regulatory news.)
_DESIG = (
    r"(?:breakthrough\s+therapy|orphan[-\s]drug|rare\s+pediatric\s+disease|RMAT"
    r"|regenerative\s+medicine\s+advanced\s+therapy)"
)
# A fresh grant, not the "AD109 has received Fast Track designation from the FDA" boilerplate that closes every
# press release ("has received", "has been granted").
_GRANT_VERB = (
    r"(?<!\bhas\s)(?<!\bhave\s)(?<!\bhad\s)(?<!\bbeen\s)(?:grant(?:s|ed|ing)?|receiv(?:e|es|ed|ing)"
    r"|award(?:s|ed|ing)?|earn(?:s|ed)?|wins?|won|secur(?:e|es|ed)|obtain(?:s|ed)|gets?|got)"
)
_VOTE_VERB = (
    r"(?:votes?|voted|voting|recommends?|recommended|backs|backed|endorses|endorsed|rejects|rejected|supports"
    r"|supported|split)"
)

_KEYWORDS: dict[str, _KwSpec] = {
    CAT_EARNINGS: (
        _words(r"results", r"quarter", r"quarterly", r"Q[1-4]", r"fiscal\s+year"),
        None,
        _words(
            r"earnings",
            r"beat\s+(?:estimates|expectations|consensus)",
            r"miss(?:ed|es)?\s+(?:estimates|expectations|consensus)",
            r"(?:quarterly|Q[1-4]|(?:first|second|third|fourth)[-\s]quarter|full[-\s]year|fiscal(?:\s+year)?"
            r"|FY\s?'?\d{2,4})\s+(?:results|earnings|revenues?|profits?|sales|deliveries|numbers|report)",
            r"(?:results|revenues?|profits?)\s+(?:for|in)\s+(?:the\s+)?(?:quarter|Q[1-4]|fiscal|full\s+year)",
            r"record\s+(?:quarter|revenues?|deliveries|profits?|earnings)",
        ),
        _words(r"EPS"),
    ),
    CAT_GUIDANCE: (
        _words(r"outlook", r"forecasts?", r"forecasting", r"forecasted"),
        None,
        _words(
            r"guidance",
            r"(?:rais|lift|boost|cut|lower|trim|slash|reaffirm|reiterat|withdr[ae]w|suspend)[a-z]*\s+"
            r"(?:(?:our|its|the|their|full[-\s]year|annual|FY\s?'?\d{2,4}|20\d{2}|quarterly|revenue|sales"
            r"|delivery|profit|earnings|margin)\s+){0,3}(?:outlook|forecasts?)",
            r"(?:full[-\s]year|annual|FY\s?'?\d{2,4}|20\d{2}|quarterly|revenue|sales|delivery|profit|earnings)"
            r"\s+(?:outlook|forecasts?)",
            r"(?:outlook|forecasts?)\s+(?:raised|lifted|boosted|cut|lowered|trimmed|slashed|reaffirmed"
            r"|withdrawn|suspended)",
        ),
        None,
    ),
    CAT_M_AND_A: (
        _words(r"welcome\s+[\w-]+(?:\s+[\w-]+)?\s+to\s+the\s+(?:[\w-]+\s+)?family"),
        None,
        _words(
            r"acquisitions?",
            r"acquir(?:e|es|ed|ing)",
            r"mergers?",
            r"merg(?:e|es|ed|ing)\s+with",
            r"buy-?outs?",
            r"takeovers?",
            # "take private", "taking it private", "taking Tesla private" (not "take your private jet")
            r"tak(?:e|es|ing|en)\s+(?:[\w-]+\s+){0,2}?private" + _PRIVATE_NOT_NOUN,
            r"go(?:es|ing)?\s+private" + _PRIVATE_NOT_NOUN,
            r"funding\s+secured",
            r"tender\s+offer",
            r"definitive\s+agreement",
            r"to\s+be\s+acquired",
        ),
        _words(r"M&A", r"LBO"),
    ),
    CAT_EXEC_CHANGE: (
        _words(
            r"board\s+of\s+directors",
            r"appoint(?:s|ed|ing|ment)?",
            r"leadership\s+(?:change|changes|transition)",
            r"successors?",
            r"succession",
        ),
        _words(r"CEO", r"CFO", r"COO", r"CTO"),
        _words(
            r"step(?:s|ped|ping)?\s+(?:down|aside)",
            r"resign(?:s|ed|ing|ation)?",
            r"(?:new|interim|incoming|next|acting)\s+" + _ROLE,
            r"(?:named|appoint(?:s|ed)?|promoted|elected|hired)\s+(?:[\w.'-]+\s+){0,3}?(?:as\s+)?(?:the\s+|our\s+)?"
            r"(?:new\s+)?" + _ROLE,
            r"(?:retir(?:e|es|ed|ing|ement)|depart(?:s|ed|ing|ure)|leav(?:e|es|ing)|left|fired|ousted|removed)"
            r"\s+(?:[\w.'-]+\s+){0,3}?(?:as\s+)?(?:the\s+)?" + _ROLE,
            r"(?:CEO|CFO|COO|CTO)\s+(?:transition|succession|departure|change|search)",
            r"succession\s+plan(?:s|ning)?",
            r"(?:pass(?:es|ed|ing)?|hand(?:s|ed|ing)?)\s+(?:over\s+)?the\s+(?:baton|reins|torch)",
            r"(?:my|his|her|their)\s+successor",  # not "the successor to the iPhone 15"
            r"successor\s+(?:as\s+)?(?:the\s+)?" + _ROLE,
            r"ousted",
        ),
        None,
    ),
    CAT_PARTNERSHIP: (
        _words(
            r"partnerships?",
            r"partner(?:s|ed|ing)?",
            r"collaborat(?:e|es|ed|ing|ion|ions)",
            r"deals?",
            r"joint\s+venture",
            # licensing (biopharma / tech): +1 like any partnership word. A licence agreement needs a counterparty
            # ("license agreement with Shionogi"): "our new Enterprise License Agreement saves customers 30%" or
            # "Llama ships under a new license agreement" is product licensing, not a deal.
            r"(?<!user\s)(?<!user-)(?<!software\s)licen[cs](?:e|ing)\s+(?:agreements?|deals?|pacts?)\s+(?:with|between)",
            r"(?:exclusive|global|worldwide)\s+(?:licen[cs]e(?!\s+to\s+operate)"
            r"|(?:development\s+and\s+)?commerciali[sz]ation\s+rights)",
            r"ex-U\.?S\.?\s+(?:rights|licen[cs]e)",
            r"commerciali[sz]ation\s+(?:rights|agreements?)",
            r"(?:tiered|double[-\s]digit|single[-\s]digit)\s+royalt(?:y|ies)",
            r"royalt(?:y|ies)\s+on\s+(?:net\s+)?sales",
            r"co-?promot(?:e|ion)\s+agreements?",
            r"option\s+to\s+(?:in-?)?licen[cs]e",
        ),
        None,
        # deal economics with a deal-size amount ("$150M upfront", "up to $400 million in milestones"): a precise
        # match counts +2 instead of +1. Consumer prices stay out ("an upfront payment of $299 for the kit",
        # "milestone payments to our suppliers: $1 billion paid early").
        _lead_words(
            r"[A-Za-z$]",
            _DEAL_AMOUNT + r"\s+(?:in\s+)?up[-\s]?front",
            r"up[-\s]?front\s+(?:cash\s+)?(?:payments?|fees?|considerations?)\s+of\s+"
            r"(?:US\s?|USD\s?|approximately\s+|about\s+)?" + _DEAL_AMOUNT,
            r"milestone\s+payments?\s+of\s+(?:up\s+to\s+)?(?:US\s?|USD\s?|approximately\s+|about\s+)?" + _DEAL_AMOUNT,
            r"(?:up\s+to\s+)?" + _DEAL_AMOUNT + r"\s+in\s+(?:[\w-]+\s+){0,4}?milestones?",
        ),
        None,
    ),
    CAT_LAYOFFS: (
        _words(
            r"lay-?offs?",
            r"laid\s+off",
            r"lay(?:ing)?\s+off",
            r"job\s+cuts?",
            r"cutting\s+jobs",
            r"restructur(?:e|es|ed|ing)",
            r"workforce\s+reductions?",
            r"headcount\s+reductions?",
        ),
        None,
        None,
        None,
    ),
    CAT_PRODUCT: (
        _words(
            r"launch(?:es|ed|ing)?",
            r"unveil(?:s|ed|ing)?",
            r"announc(?:e|es|ed|ing|ement|ements)",
            r"releas(?:e|es|ed|ing)",
            r"introduc(?:e|es|ed|ing)",
            r"all-new",
            r"available\s+now",
            r"now\s+available",
            r"ship(?:s|ped|ping)?",
            r"pre-?orders?",
            r"rolling\s+out",
            r"roll(?:s|ed)?\s+out",
        ),
        None,
        # a prescription-drug launch: a precise match counts +2 instead of +1. A plain "commercial launch" is also
        # a rocket launch ("SpaceX's 140th commercial launch") or a chip GA, and stays a broad "launch" (+1).
        _alpha_words(
            r"(?:now\s+)?(?:commercially\s+)?available\s+(?:by|with\s+a|via)\s+prescription",
            r"(?:U\.?S\.?|EU|European|Japanese)\s+commercial\s+launch\s+of",
        ),
        None,
    ),
    CAT_REGULATORY: (
        _words(r"probes?", r"approval", r"approv(?:ed|es)"),
        None,
        _words(
            r"antitrust",
            r"regulators?",
            r"regulatory",
            r"investigations?",
            r"investigat(?:e|es|ed|ing)",
            r"lawsuits?",
            r"su(?:es|ed|ing)",
            r"subpoena(?:s|ed)?",
            # "export ban", "export-ban", "banned" - but not the "Ray-Ban" brand
            r"(?<!ray-)(?<!ray\s)ban(?:s|ned)?",
            r"tariffs?",
            # "probe" only with a regulator / investigation word next to it ("space probe" stays weak)
            r"(?:regulator[a-z]*|government|federal|antitrust|criminal|congressional|senate|state|EU|"
            r"SEC|FTC|DOJ|FCC|FAA|NHTSA|FDA|CFTC)\s+probes?",
            r"probes?\s+(?:into|by\s+(?:the\s+)?(?:SEC|FTC|DOJ|FCC|FAA|NHTSA|FDA|CFTC|EU|regulators?))",
            r"(?:FDA|FAA|FCC|NHTSA|SEC|EU|regulatory|government)\s+approv(?:al|ed|es)",
            r"approv(?:al|ed)\s+(?:by|from)\s+(?:the\s+)?(?:FDA|FAA|FCC|NHTSA|SEC|EU|regulators?|government)",
        ),
        _words(r"FDA", r"SEC", r"FTC", r"DOJ", r"CFTC", r"NHTSA", r"FCC", r"FAA", r"EU\s+Commission"),
    ),
    CAT_CLINICAL: (
        # broad: a trial is mentioned, no outcome ("enrolling a Phase 3 trial", "topline data expected in Q2")
        _alpha_words(
            r"clinical\s+trials?",
            _PHASE + r"\s+(?:clinical\s+)?(?:trials?|stud(?:y|ies)|programs?|data|results?|read-?outs?)",
            r"(?:pivotal|registrational)\s+(?:trials?|stud(?:y|ies))",
            _ENDPOINT,
            r"top-?line\s+(?:results?|data)",
            r"interim\s+analys[ie]s",
            r"first\s+patients?\s+dosed",
        ),
        None,
        # precise: an outcome or a stop, with clinical context inside the match. Generic English stays out: "I
        # stopped dosing ketamine", "FSD free trial enrollment is paused", "Positive trial results for Robotaxi",
        # "Robust Phase 1 results at Stargate", "topline data from the gpt-6 eval", "BFCM topline data: $14.6B".
        _alpha_words(
            # "met its primary endpoint", "failed to meet the primary endpoint", "missed both co-primary endpoints"
            r"(?:met|meets|meeting|achieved|achieves|achieving|hit|hits|missed|miss(?:es|ing)?"
            r"|fail(?:s|ed|ing)?\s+to\s+(?:meet|achieve|reach|hit)|did(?:\s+not|n['’]t)\s+(?:meet|achieve|reach|hit)"
            r"|does(?:\s+not|n['’]t)\s+meet)\s+(?:(?:its|the|both|all|our|their|two|key|study['’]s|trial['’]s)\s+){0,2}"
            + _ENDPOINT,
            _ENDPOINT + r"\s+(?:was|were|has\s+been|have\s+been|is|are)\s+(?:not\s+)?(?:met|achieved|missed)",
            # "statistically significant reduction in AHI on the primary endpoint"
            r"(?:statistically|clinically)\s+significant\s+(?:[\w-]+\s+){0,5}?(?:on|in|for|across|at)\s+"
            r"(?:(?:the|its|both|all|key|each)\s+){0,2}" + _ENDPOINT,
            # "positive topline results", "positive top-line Phase 3 data" (not a bare "topline data": revenue in
            # finance, or "topline data expected in 2026")
            _OUTCOME_TOPLINE + r"\s+top-?line\s+(?:" + _PHASE + r"\s+)?(?:results?|data|read-?outs?)",
            # "positive Phase 3 trial results"; "positive Phase 3 results" only with a clinical word later in the
            # same sentence
            _OUTCOME + r"\s+(?:(?:interim|final|pivotal)\s+)?" + _PHASE + r"\s+(?:clinical\s+)?" + _TRIAL
            + r"\s+(?:results?|data|read-?outs?)",
            _OUTCOME + r"\s+(?:(?:interim|final|pivotal)\s+)?" + _PHASE + r"\s+(?:results?|data|read-?outs?)"
            + _CLINICAL_AHEAD,
            _OUTCOME + r"\s+(?:(?:interim|final)\s+)?clinical\s+(?:results?|data|read-?outs?)",
            # ("Encouraging pivotal data from NVIDIA's Rubin bring-up" stays out)
            _OUTCOME + r"\s+(?:(?:interim|final)\s+)?(?:pivotal|trial)\s+(?:results?|data|read-?outs?)"
            + _CLINICAL_AHEAD,
            # "topline results from SynAIRgy Phase 3 Trial", "topline data from its pivotal study"
            r"top-?line\s+(?:results?|data|read-?outs?)\s+(?:from|of|in)\s+"
            r"(?:(?:the|its|our|an?|two|both|first|second)\s+)?(?:[\w-]+\s+){0,4}?" + _TRIAL,
            # "positive results from the Phase 3 trial"; a bare trial needs a clinical word later in the sentence
            # ("positive results from the SynAIRgy trial in patients with OSA"), and a bare "study" never counts
            # ("Encouraging results from our study of ChatGPT Health" is research news)
            r"(?:" + _OUTCOME + r"|interim)\s+(?:results?|data|read-?outs?)\s+(?:from|of|in)\s+"
            r"(?:(?:the|its|our|an?|two|both|first|second)\s+)?(?:[\w-]+\s+){0,3}?"
            r"(?:(?:clinical|pivotal|registrational|late-stage|" + _PHASE + r")\s+(?:[\w-]+\s+){0,2}?" + _TRIAL
            + r"|(?<!free\s)(?<!free-)trials?" + _CLINICAL_AHEAD + r")",
            # "Phase 3 trial met", "Phase 2b study of AD109 failed"
            _PHASE + r"\s+(?:clinical\s+)?" + _TRIAL + r"\s+(?:(?:of|for|in|evaluating)\s+[\w-]+\s+)?"
            # (not "showed": "Phase 2 trials of FSD showed a big improvement")
            r"(?:met|meets|succeeded|succeeds|failed|fails|missed|misses|achieved|achieves"
            r"|was\s+" + _TRIAL_STOPPED + r"|" + _TRIAL_STOPPED + r")",
            r"clinical\s+holds?",
            # "stopped early for efficacy", "halted the AD109 trial for futility"
            r"(?:stop(?:s|ped|ping)?|halt(?:s|ed|ing)?|terminat(?:e|es|ed|ing)|end(?:s|ed)?|unblind(?:s|ed|ing)?)\s+"
            r"(?:(?:the|its|our|a)\s+)?(?:(?:[\w-]+\s+){0,2}?" + _TRIAL + r"\s+)?(?:early\s+)?"
            r"(?:for|due\s+to|because\s+of|on)\s+(?:overwhelming\s+)?(?:futility|efficacy)",
            r"(?:for|due\s+to)\s+futility",
            r"futility\s+(?:analysis|analyses|boundary|stop|stopping|criteria)",
            # "dosing in the AD109 Phase 2 trial has been paused", "patient enrollment suspended" (not "I stopped
            # dosing ketamine", "free trial enrollment is paused", "study enrollment paused for the summer")
            r"(?:patient\s+(?:dosing|enrol{1,2}ment)|(?:dosing|enrol{1,2}ment)\s+(?:in|of)\s+"
            r"(?:(?:the|all|its|our)\s+)?(?:[\w-]+\s+){0,3}?(?:" + _TRIAL + r"|patients))\s+"
            r"(?:(?:is|was|has\s+been|have\s+been)\s+)?(?:(?:temporarily|voluntarily)\s+)?" + _TRIAL_STOPPED,
            # "Phase 3 trial of AD109 has been halted", "clinical program discontinued" (not "Phase 2 program paused")
            r"(?:(?:clinical|pivotal|registrational|late-stage|" + _PHASE + r")\s+" + _TRIAL + r"|clinical\s+programs?)"
            r"\s+(?:(?:of|for|in)\s+[\w-]+\s+)?(?:(?:is|was|were|has\s+been|have\s+been)\s+)?"
            r"(?:(?:temporarily|voluntarily)\s+)?" + _TRIAL_STOPPED,
            # "halts the Phase 3 trial", "pauses dosing in the AD109 trial"
            r"(?:halt|paus|suspend|stop|discontinu|terminat)(?:e|es|ed|ing|s|ped|ping)?\s+(?:(?:the|its|our|a|all)\s+)?"
            r"(?:[\w-]+\s+){0,2}?(?:clinical\s+trials?|" + _PHASE + r"\s+" + _TRIAL + r"|patient\s+dosing"
            r"|dosing\s+(?:in|of)\s+(?:(?:the|all|its|our)\s+)?(?:[\w-]+\s+){0,3}?(?:" + _TRIAL + r"|patients))",
            r"data\s+(?:and\s+)?safety\s+monitoring\s+(?:board|committee)",
            r"data\s+monitoring\s+committee",
        ),
        # case-sensitive: "Run-DMC" / "DeLorean DMC" are not a data monitoring committee
        _alpha_words(
            r"DSMB",
            r"IDMC",
            r"iDMC",
            r"DMC(?=\s+(?:has\s+|have\s+)?(?:recommend|review|conclud|found|determin))",
        ),
    ),
    CAT_FDA_ACTION: (
        # broad (a hint for Claude, no score): a mention without an event - "PDUFA is Feb 28", "on track for its
        # PDUFA date", "has a Boxed Warning", "Breakthrough Therapy designation", "FDA advisory committee"
        _alpha_words(
            r"FDA\s+(?:decision|review|filing|submission)",
            r"(?:FDA|target|regulatory)\s+action\s+dates?",
            r"boxed\s+warnings?",
            _DESIG + r"\s+(?:designations?|status)",
            r"FDA\s+(?:[\w-]+\s+){0,3}?advisory\s+(?:committee|panel)",
            r"FDA\s+panel",
            _DRUG_APP,
        ),
        _alpha_words(r"PDUFA", r"AdCom", r"ODAC", r"REMS", r"CHMP", r"Fast\s+Track\s+designation"),
        # precise: an FDA / EMA EVENT on a drug. Label statements ("Zepbound is approved for adults with obesity"),
        # safety boilerplate ("has a Boxed Warning", "the REMS program"), device pathways ("FDA cleared",
        # "authorized", "Breakthrough Device") and look-alikes ("priority review for your mortgage application",
        # "fast-track designation by the state", "court panel voted 2-1") stay out.
        _alpha_words(
            r"complete\s+response\s+letters?",
            # "refuse-to-file letter" (not "I refuse to file my taxes late")
            r"refus(?:e|al)-to-file",
            r"refus(?:e|al)\s+to\s+file\s+(?:letter|notice|decision)",
            # PDUFA events (a bare "PDUFA is Feb 28" is a reminder: broad)
            r"PDUFA\s+(?:(?:goal|target|action)\s+)?dates?\s+(?:(?:has\s+been|have\s+been|was|is|were)\s+)?"
            r"(?:set|assigned|extended|moved|pushed|delayed|postponed|changed|reset)",
            r"new\s+PDUFA\s+(?:(?:goal|target|action)\s+)?dates?",
            r"PDUFA\s+(?:(?:goal|target|action)\s+)?(?:dates?\s+)?extensions?",
            # "receives FDA approval" (an FDA "clearance" / "authorization" is a device: regulatory only)
            r"(?:receiv(?:e|es|ed|ing)|wins?|won|gain(?:s|ed|ing)?|secur(?:e|es|ed|ing)|obtain(?:s|ed|ing)?|granted"
            r"|earn(?:s|ed)?|gets?|got)\s+(?:(?:full|conditional|accelerated|traditional|U\.?S\.?|EU|European"
            r"|Japanese)\s+)?(?:(?:FDA|EMA|MHRA|PMDA|NMPA)\s+approvals?|marketing\s+(?:approvals?|authori[sz]ations?))"
            r"(?!" + _TRIAL_PERMISSION + r")",
            # "Apnimed announces U.S. FDA approval of Oxnimbi" (not "seeks FDA approval of")
            r"(?:announc(?:es|ed|ing)|celebrat(?:es|ed|ing)|confirm(?:s|ed)|reports?|reported)\s+(?:(?:the|its)\s+)?"
            r"(?:U\.?S\.?\s+)?(?:FDA|EMA)\s+approval\s+(?:of|for)(?!" + _TRIAL_PERMISSION + r")",
            # "FDA grants accelerated approval / priority review / Breakthrough Therapy designation"
            r"(?:FDA|EMA)\s+(?:has\s+)?(?:grant(?:s|ed)|award(?:s|ed))\s+(?:(?:an?|the|its)\s+)?(?:"
            r"(?:full|conditional|accelerated|traditional)\s+approval|priority\s+review|(?:" + _DESIG
            + r"|fast[-\s]track)\s+(?:designations?|status))",
            r"(?:approv(?:ed|es|al)|accept(?:s|ed|ance))\s+(?:of\s+)?(?:(?:the|its|our|an?)\s+)?(?:[\w'’-]+\s+){0,2}?"
            + _DRUG_APP,
            r"(?:submit(?:s|ted|ting)|submission\s+of|files|filed|filing\s+of|resubmit(?:s|ted|ting)|resubmission\s+of)"
            r"\s+(?:(?:an?|the|its|our)\s+)?(?:[\w'’-]+\s+){0,2}?" + _DRUG_APP,
            # "approved with a boxed warning", "FDA adds a boxed warning"
            r"(?<!\bis\s)(?<!\bare\s)approv(?:ed|es|al)\s+with\s+(?:an?\s+)?(?:boxed|black[-\s]box)\s+warnings?",
            r"(?:add(?:s|ed|ing)?|requir(?:e|es|ed|ing)|impos(?:e|es|ed|ing)|mandat(?:e|es|ed|ing)|issu(?:e|es|ed|ing))"
            r"\s+(?:(?:an?|the|its)\s+)?(?:new\s+)?(?:boxed|black[-\s]box)\s+warnings?",
            # advisory committees: a vote or a scheduled meeting of an FDA / drug committee ("No FDA advisory
            # committee is planned", "the White House AI advisory committee recommends" stay out)
            r"(?:FDA|FDA['’]s|ODAC|agency['’]s|drugs?|therap(?:y|ies))\s+(?:[\w-]+\s+){0,3}?advisory\s+"
            r"(?:committee|panel)\s+(?:meeting\s+)?" + _VOTE_VERB,
            r"(?:FDA|agency)\s+(?:has\s+|will\s+)?(?:schedul(?:es|ed|ing)|conven(?:e|es|ed|ing)|plans|planned|calls"
            r"|called|sets)\s+(?:(?:an?|its|the)\s+)?(?:[\w-]+\s+){0,2}?advisory\s+(?:committee|panel)",
            r"FDA\s+(?:advisory\s+|expert\s+)?panel\s+" + _VOTE_VERB,
            # "Panel votes 9-4 in favor of Apnimed's sleep apnea pill" (not "Senate panel votes 12-10 ..." or "the
            # appeals court panel voted 2-1 in favor of Tesla")
            r"(?<!senate\s)(?<!house\s)panel\s+vot(?:e|es|ed)\s+\d{1,2}\s?(?:-|–|to)\s?\d{1,2}\s+"
            r"(?:in\s+favou?r\s+of|against|to\s+(?:recommend|support|approve|back))\s+(?:[\w'’-]+\s+){0,5}?"
            r"(?:drugs?|pills?|therap(?:y|ies)|treatments?|medicines?|medications?|vaccines?|approval)",
            # a fresh designation ("AD109 granted Breakthrough Therapy designation")
            _GRANT_VERB + r"\s+(?:(?:an?|the|its|U\.?S\.?|EU)\s+)?(?:(?:FDA|EMA)\s+(?:" + _DESIG
            + r"|fast[-\s]track)|" + _DESIG + r")\s+(?:designations?|status)",
            _DESIG + r"\s+designations?\s+(?:granted|awarded)\s+(?:to|for)",
            # priority review of a drug application
            r"priority\s+review\s+vouchers?",
            r"priority\s+review\s+(?:to|for|of)\s+(?:(?:the|its|our|an?)\s+)?(?:[\w'’-]+\s+){0,2}?" + _DRUG_APP,
            r"(?:receiv(?:e|es|ed|ing)|wins?|won|gain(?:s|ed)?|secur(?:e|es|ed)|obtain(?:s|ed)|gets?|got|granted)\s+"
            r"(?:FDA|EMA)\s+priority\s+review",
            # accelerated approval of a drug ("the NRC's accelerated approval pathway", "Texas granted accelerated
            # approval of the permits" stay out)
            r"(?:FDA|EMA)\s+(?:[\w-]+\s+){0,2}?accelerated\s+approval"
            r"(?!\s+(?:pathways?|programs?|programmes?|process|rules?|framework))",
            r"approv(?:ed|al)\s+under\s+(?:the\s+)?accelerated\s+approval",
            r"accelerated\s+approval\s+from\s+(?:the\s+)?(?:U\.?S\.?\s+)?(?:FDA|EMA)",
            r"conditional\s+marketing\s+authori[sz]ation",
            # label expansions of a drug ("Monster Energy Records label expansion", "Reign label extension" stay out)
            r"indication\s+(?:expansion|extension)",
            r"(?:FDA|EMA|sNDA|sBLA|supplemental)\s+(?:[\w-]+\s+){0,3}?(?:label|indication)\s+(?:expansion|extension"
            r"|update)",
            r"(?:label|indication)\s+(?:expansion|extension)\s+(?:of\s+[\w'’-]+\s+)?to\s+include",
            r"expand(?:s|ed|ing)?\s+(?:(?:the|its)\s+)?(?:label|indication)\s+(?:of\s+[\w'’-]+\s+)?to\s+include",
            r"(?:FDA|EMA)\s+(?:has\s+)?(?:expand(?:s|ed)|broaden(?:s|ed))\s+(?:(?:the|its)\s+)?(?:[\w'’-]+\s+){0,2}?"
            r"(?:approval|label|indication)",
            # review extensions ("extending the review period for Copilot feedback", "pushed back the goal date for
            # Optimus V3", "FERC extended the review of the interconnection application" stay out)
            r"(?:extend(?:s|ed|ing)?|extension\s+of|delay(?:s|ed|ing)?|push(?:es|ed)?\s+back)\s+(?:(?:the|its)\s+)?"
            r"(?:PDUFA|FDA\s+review)",
            r"extend(?:s|ed|ing)?\s+(?:(?:the|its)\s+)?review\s+of\s+(?:(?:the|its)\s+)?(?:[\w'’-]+\s+){0,2}?"
            + _DRUG_APP,
            r"major\s+amendment\s+to\s+(?:(?:the|its)\s+)?(?:[\w'’-]+\s+)?" + _DRUG_APP,
        ),
        _alpha_words(
            # "FDA approves", "EMA has accepted" (not "is FDA approved to prevent headaches", "an FDA approved
            # treatment", "the 200-day EMA rejected bitcoin", "FDA approved ... for its first human implant")
            r"(?<!\b[Ii]s\s)(?<!\bare\s)(?<!\b[Aa]n\s)(?<!\bonly\s)(?<!\bfirst\s)(?<!\bbe\s)(?<!\bbeen\s)"
            + _REGULATOR_CS + r"\s+(?i:has\s+|have\s+)?(?i:approves|approved" + _NOT_ADJECTIVE
            + r"|accept(?:s|ed)|reject(?:s|ed))(?!(?i:" + _TRIAL_PERMISSION + r"))",
            # complete response letters ("$CRL beats Q3 estimates", "update your CRL distribution points" stay out)
            r"(?:FDA\s+(?i:issu(?:e|es|ed|ing)|sends?|sent|hands?|handed)|(?i:receiv(?:e|es|ed|ing)|gets?|got))\s+"
            r"(?:(?i:an?|the|its|a\s+second)\s+)?(?<!\$)CRL",
            r"(?<!\$)CRL\s+(?i:from\s+(?:the\s+)?)FDA",
            r"(?:AdCom|ODAC)\s+(?i:" + _VOTE_VERB + r")",
            r"(?i:schedul(?:es|ed|ing)|conven(?:e|es|ed|ing)|plans|planned|calls|called|sets)\s+(?:(?i:an?|its|the)\s+)?"
            r"(?:AdCom|ODAC)",
            r"sNDA",
            r"sBLA",
            # a drug application with drug context: "NDA for AD109 accepted", "Apnimed's NDA accepted", "submits the
            # NDA for AD109" (not "NDA was approved by legal", "they accepted our NDA", "signed an NDA")
            r"(?:NDA|BLA|MAA)\s+(?i:for|of)\s+[\w'’-]+\s+(?:(?i:is|was|has\s+been|have\s+been)\s+)?(?i:" + _APP_EVENT
            + r")",
            r"[A-Za-z][A-Za-z0-9]{0,30}['’]s\s+(?:NDA|BLA|MAA)\s+(?:(?i:for|of)\s+[\w-]+\s+)?"
            r"(?:(?i:is|was|has\s+been)\s+)?(?i:" + _APP_EVENT + r")",
            r"(?:BLA|MAA)\s+(?:(?i:is|was|has\s+been)\s+)?(?i:" + _APP_EVENT + r")",
            r"(?i:submit(?:s|ted|ting)|submission\s+of|files|filed|filing\s+of|resubmit(?:s|ted|ting)|resubmission\s+of)"
            r"\s+(?:(?i:an?|the|its|our)\s+)?(?:[\w'’-]+\s+){0,2}?(?:" + _DRUG_APP_CS_NO_NDA
            + r"|NDA(?=\s+(?i:for\s|to\s+(?:the\s+)?FDA|with\s+(?:the\s+)?FDA|seeking)))",
            r"(?i:approv(?:ed|es|al)|accept(?:s|ed|ance))\s+(?i:of\s+)?(?:(?i:the|its|our|an?)\s+)?(?:[\w'’-]+\s+){0,2}?"
            r"(?:" + _DRUG_APP_CS_NO_NDA + r"|NDA(?=\s+(?i:for|of)\s))",
            _DRUG_APP_CS + r"\s+(?:[\w'’-]+\s+){0,4}?(?i:under\s+priority\s+review)",
            r"(?i:priority\s+review\s+(?:to|for|of)\s+(?:(?:the|its|our|an?)\s+)?)(?:[\w'’-]+\s+){0,2}?" + _DRUG_APP_CS,
            r"(?i:extend(?:s|ed|ing)?\s+(?:(?:the|its)\s+)?review\s+of\s+(?:(?:the|its)\s+)?)(?:[\w'’-]+\s+){0,2}?"
            + _DRUG_APP_CS,
            r"(?i:major\s+amendment\s+to\s+(?:(?:the|its)\s+)?)(?:[\w'’-]+\s+)?" + _DRUG_APP_CS,
            # a fresh "Fast Track" designation, written as the FDA writes it ("Fast-track designation for H-1B
            # renewals" and "granted fast-track designation by the state" are permits)
            r"(?i:" + _GRANT_VERB + r")\s+(?:(?i:an?|the|its)\s+)?(?:FDA\s+)?Fast\s+Track\s+(?i:designations?|status)",
            # EU opinions
            r"(?:CHMP|" + _EMA + r")\s+(?i:(?:positive|negative|favou?rable)\s+)?(?i:opinion)",
            r"(?i:positive|negative|favou?rable)\s+(?:CHMP|EMA)\s+(?i:opinion)",
            r"CHMP\s+(?i:recommends?|recommended|adopt(?:s|ed)|backs|backed)",
            r"(?i:approv(?:ed|es|al))\s+with\s+(?:(?i:an?)\s+)?REMS",
        ),
    ),
    CAT_CORPORATE_STATUS: (
        # not "licensing deal" / "license agreement" / "exclusive license" (partnership) or "in-licensing";
        # "an exclusive license to operate" is an operating licence
        _words(
            r"(?<!in-)(?<!out-)(?<!cross-)(?:(?<!exclusive\s)licen[cs](?:e|es|ed|ing)|licen[cs]es?(?=\s+to\s+operate))"
            r"(?!\s+(?:agreements?|deals?|pacts?|partners?|partnerships?|fees?|payments?|rights|revenues?)\b)",
            r"charter",
        ),
        None,
        _words(
            r"bankrupt(?:cy|cies)?",
            r"chapter\s+(?:7|11|15)\s+(?:bankruptcy|protection|filing|proceedings?|restructuring)",
            r"(?:file[sd]?|filing)\s+(?:for\s+)?chapter\s+(?:7|11|15)",
            r"insolven(?:t|cy)",
            r"receivership",
            r"going\s+concern",
            r"de-?list(?:s|ed|ing)?",
            r"(?:bank(?:ing)?|national\s+bank|trust|federal|state|OCC)\s+charters?",
            r"charters?\s+(?:approval|application|approved|granted)",
            r"(?:banking|bank|money[-\s]transmitter|broker-?dealer|exchange|operating|export|spectrum|gaming"
            r"|crypto|e-?money|payments?|insurance|lending)\s+" + _LICENSE,
            _LICENSE + r"\s+(?:approved|granted|revoked|suspended|renewed)",
            r"(?:granted|received|obtained|secured|won|revoked|suspended|lost)\s+(?:a|an|our|the|its|their)?\s*"
            r"(?:[\w-]+\s+){0,2}?" + _STATUS_LICENSE,
            r"bitlicense",
            r"defaulted\s+on",
        ),
        None,
    ),
    CAT_INCIDENT: (
        _words(
            r"hacks?", r"hackers?", r"hacking", r"breach(?:ed|es)?", r"halt(?:s|ed|ing)?", r"disruptions?",
            r"explosion",
        ),
        None,
        _words(
            r"outages?",
            r"hacked",
            # not "BTC breached $100k": only a breach of systems / data
            r"(?:security|data|privacy|network|system|systems)\s+breach(?:es)?",
            r"breach(?:ed)?\s+(?:of\s+)?(?:our|their|its|the)?\s*(?:systems?|security|networks?|data|servers?"
            r"|databases?|accounts?)",
            r"cyber-?attacks?",
            r"ransomware",
            r"data\s+leaks?",
            # "Our systems are down right now", "the app is down"
            # ("services were down 3% YoY" is a business metric, not an outage)
            r"(?:systems?|site|website|apps?|services?|servers?|platform|network|api|exchange|login)"
            r"\s+(?:is|are|was|were|went|goes|going)\s+(?:currently\s+|completely\s+|partially\s+)?down"
            r"(?!\s*(?:\d|\$|by\b|from\b|to\b|yoy\b|year\b|on\s+the\s+(?:year|quarter)))",
            r"(?:safety|voluntary|product|vehicle|massive|software|global|nationwide)\s+recalls?",
            r"recall(?:s|ed|ing)?\s+(?:of\s+)?(?:about\s+|over\s+|nearly\s+|some\s+|all\s+)?"
            r"(?:\d[\d,.]*\s?(?:[KkMm]|thousand|million)?\s+)?"
            r"(?:vehicles|cars|trucks|units|products|devices|batteries|cybertrucks)",
            r"(?:pause[sd]?|pausing|halt(?:s|ed|ing)?|stop(?:s|ped|ping)?|suspend(?:s|ed|ing)?|shut(?:s|ting)?\s+down)"
            r"\s+(?:[\w-]+\s+){0,3}?production",
            r"production\s+(?:[\w-]+\s+){0,4}?(?:is|was|has\s+been|have\s+been|remains|will\s+be)\s+"
            r"(?:temporarily\s+)?(?:paused|halted|stopped|suspended|shut\s+down)",
            r"(?:factory|plant|facility|gigafactory|fab|refinery|warehouse)\s+(?:fire|explosion|shutdown|closure)",
            r"fire\s+at\s+(?:our\s+|the\s+)?(?:[\w-]+\s+){0,2}?(?:factory|plant|facility|gigafactory|giga|fab|warehouse)",
            r"trading\s+(?:halt(?:ed)?|suspended)",
            r"halted\s+trading",
        ),
        None,
    ),
    CAT_BUYBACK: (
        _words(r"buy-?backs?", r"buy(?:ing)?\s+back", r"repurchas(?:e|es|ed|ing)"),
        None,
        None,
        None,
    ),
    CAT_CAPITAL_RETURN: (
        _words(r"dividends?"),
        None,
        _words(
            r"(?:quarterly|special|annual|cash|stock|first(?:-ever)?|inaugural|initial|regular|monthly"
            r"|increas[a-z]*|rais[a-z]*|boost[a-z]*|hik[a-z]*|cut[a-z]*|slash[a-z]*|suspend[a-z]*|eliminat[a-z]*"
            r"|declar[a-z]*|initiat[a-z]*|reinstat[a-z]*)\s+"
            r"(?:(?:the|our|its|a|an|of|per[-\s]share|common|preferred|\$?\d[\d.,]*%?)\s+){0,3}?dividends?",
            r"dividends?\s+(?:of|per|increase|hike|cut|payment|payout|yield|growth)",
            r"(?:stock|share)\s+splits?",
            r"\d+[-\s](?:for|to)[-\s]\d+(?:\s+(?:stock|share|forward|reverse))?\s+splits?",
            r"(?:reverse|forward)\s+(?:stock\s+)?splits?",
            r"capital\s+returns?",
            r"return(?:s|ed|ing)?\s+(?:\$?\d[\d.,]*\s?(?:[KMBT]|billion|million)?\s+(?:of\s+)?)?"
            r"capital\s+to\s+shareholders",
        ),
        None,
    ),
    CAT_OFFERING: (
        _words(r"offerings?", r"notes"),
        None,
        _words(
            r"(?:public|secondary|follow-on|stock|share|equity|debt|bond|notes?|convertible|registered\s+direct"
            r"|at-the-market|ATM|initial\s+public|private|preferred|rights|token)\s+offerings?",
            # "raise" only in the capital-raising sense: not "raise guidance" / "raise prices"
            r"rais(?:e|es|ed|ing)\s+(?:~|about\s+|over\s+|up\s+to\s+)?"
            r"(?:\$?\s?\d[\d,.]*\s?[A-Za-z]*|capital|funds|money|cash|equity|debt|a\s+round)",
            r"(?:capital|equity)\s+raises?",
            r"fundrais(?:e|es|ed|ing)",
            r"convertibles?",
            r"(?:convertible|senior|secured|unsecured|exchangeable)\s+(?:[\w-]+\s+)?notes",
            r"notes\s+due",
            r"\d+(?:\.\d+)?\s?%\s+(?:[\w-]+\s+){0,2}?notes",
            r"dilution",
            r"dilutive",
            r"at-the-market",
            r"priced\s+(?:an?\s+|its\s+|our\s+)?(?:[\w$.,%-]+\s+){0,4}?offering",
            # "prices $200 million offering" (a dollar amount: not "lower prices on our cloud offering")
            r"(?:prices|pricing\s+of)\s+(?:an?\s+|its\s+|our\s+|the\s+)?(?:upsized\s+)?" + _DEAL_AMOUNT
            + r"\s+(?:[\w-]+\s+){0,3}?offerings?",
            r"underwritten\s+(?:public\s+|registered\s+direct\s+)?offerings?",
            # lock-up expiry: insider shares become sellable ("lock-up", "lockup" or "IPO lock up"; a spaced "lock
            # up ends at 6pm" is closing time)
            r"(?:lock-ups?|lockups?|(?:IPO|insider|shareholder|share)\s+lock[-\s]?ups?)\s+"
            r"(?:expir(?:y|ation|ations|es|ed|ing)|release|ends?|ending|period\s+(?:ends?|ending|expir(?:y|ation|es|ed|ing)))",
            r"(?:expir(?:y|ation|es|ing)|end)\s+of\s+(?:the\s+|its\s+|their\s+)?(?:[\w-]+\s+){0,2}?(?:lock-ups?|lockups?)",
        ),
        _words(r"ATM", r"IPO", r"IPOs"),
    ),
}

_BTC_WORD = r"(?:BTC|bitcoins?|₿|sats)"
# "acquired 1,000 BTC", "bought ~5,000 bitcoin", "purchased an additional 7,390 BTC"
_BITCOIN_PURCHASE_RE = re.compile(
    _LB + r"(?:acquir(?:ed|es|e|ing)|bought|purchas(?:ed|es|e|ing)|buying|added)" + _RB
    + r"[^.\n!?]{0,80}?" + _LB + _BTC_WORD + r"(?![A-Za-z0-9_])",
    re.IGNORECASE,
)
# "We sold some bitcoin this week", "selling 704 BTC" (a sale by a treasury company is big news).
# Short gap, no commas: "Sell a kidney if you must, but keep the bitcoin" is not a sale.
_BITCOIN_SALE_RE = re.compile(
    _LB + r"(?:sold|sell(?:s|ing)?|dump(?:s|ed|ing)?|offload(?:s|ed|ing)?|liquidat(?:e|es|ed|ing))" + _RB
    + r"[^.,;\n!?]{0,40}?" + _LB + _BTC_WORD + r"(?![A-Za-z0-9_])",
    re.IGNORECASE,
)
# "never sell your bitcoin", "don't sell", "no plans to sell": the classic HODL post, not a sale
_NEGATION_BEFORE_RE = re.compile(
    r"(?:\b(?:never|not|no|don['’]?t|won['’]?t|wouldn['’]?t|shouldn['’]?t|cannot|can['’]?t|nobody|stop)"
    r"|n['’]t)(?:\s+[\w'’]+){0,2}\s*$",
    re.IGNORECASE,
)
# "1,000 BTC", "₿ 21,000", "21K bitcoin", "21,000 ₿"
_BITCOIN_AMOUNT_RE = re.compile(
    r"(?:" + _NUM_LB + r"\d[\d,]*(?:\.\d+)?\s*(?:[KkMm]\b)?\s*(?:BTC|bitcoins?)" + _RB + r")"
    + r"|(?:₿\s*\d[\d,]*(?:\.\d+)?)"
    + r"|(?:" + _NUM_LB + r"\d[\d,]*(?:\.\d+)?\s*₿)",
)
_BITCOIN_SYMBOL_RE = re.compile(r"₿")

_MAGNITUDE = r"(?:[KMBT]|k|bn|mn|mm|thousand|million|billion|trillion)"
_UNIT_WORDS = (
    r"units|vehicles|cars|deliveries|robots|customers|users|subscribers|members|accounts|"
    r"GPUs|chips|wafers|servers|employees|jobs|shares|launches|satellites|BTC|bitcoins?"
)
_NUMBER_RES: tuple[Pattern[str], ...] = (
    # $5B, $500 million, $1.2bn, $20,000,000
    re.compile(r"\$\s?\d[\d,]*(?:\.\d+)?\s?" + _MAGNITUDE + _RB, re.IGNORECASE),
    re.compile(r"\$\s?\d{1,3}(?:,\d{3})+(?:\.\d+)?" + _RB),
    # 25%, 3.5 % (but "100%" alone is almost always "totally agree" - handled below)
    re.compile(r"(?<![\d.])\d+(?:\.\d+)?\s?%"),
    # 1.2M units, 500,000 vehicles, 1,000 BTC
    re.compile(
        _NUM_LB + r"\d[\d,]*(?:\.\d+)?\s?" + _MAGNITUDE + r"?\s+(?:" + _UNIT_WORDS + r")" + _RB,
        re.IGNORECASE,
    ),
    # 10 GW, 1.5 GWh, 50 EH/s
    re.compile(_NUM_LB + r"\d[\d,]*(?:\.\d+)?\s?(?:GW|MW|TW|GWh|MWh|TWh|kWh|EH/s)" + _RB),
    # 500 million, 2 billion
    re.compile(_NUM_LB + r"\d[\d,]*(?:\.\d+)?\s?(?:million|billion|trillion)" + _RB, re.IGNORECASE),
)
_AGREEMENT_PERCENT_RE = re.compile(r"^100\s?%$")

# --------------------------------------------------------------------------- company aliases

#: Single-word aliases that are also ordinary English words. They only match when capitalized
#: exactly as written in the watchlist ("Meta", not "so meta"; "Base", not "back to base").
#: Mark any other alias as exact-case with a leading "=" in company_names, e.g. "=Gaudi".
#: Known collision that case cannot fix: "Gemini" (Google's model vs the crypto exchange).
COMMON_WORD_ALIASES: frozenset[str] = frozenset(
    {
        "alphabet",
        "azure",
        "base",
        "copilot",
        "gemini",
        "instinct",
        "intel",
        "llama",
        "meta",
        "optimus",
        "rubin",
        "strategy",
    }
)
#: ... and these additionally never count as the first word of a sentence, where every word is
#: capitalized ("Strategy matters.", "Base case: ...").
MID_SENTENCE_ONLY_ALIASES: frozenset[str] = frozenset({"base", "strategy"})
_EXACT_CASE_MARK = "="
_SENTENCE_LEAD_CHARS = " \t\"'“‘(*•-–—"
_SENTENCE_END_CHARS = ".!?:;\n\r…"

_MAX_TICKER_REASONS = 6
_MAX_NUMBER_REASONS = 3
_MAX_NUMBER_MATCHES_PER_PATTERN = 50
_MAX_REASON_SNIPPET = 60


@dataclass
class _Scan:
    """Mutable accumulator for one evaluation."""

    reasons: list[str] = field(default_factory=list)
    categories: list[str] = field(default_factory=list)
    strong: set[str] = field(default_factory=set)  # categories with a precise match (or no precise patterns)
    precise: set[str] = field(default_factory=set)  # categories where a precise pattern matched
    tickers: list[str] = field(default_factory=list)
    has_numbers: bool = False

    def add_category(self, cat: str, strong: bool = True, precise: bool = False) -> None:
        if cat not in self.categories:
            self.categories.append(cat)
        if strong:
            self.strong.add(cat)
        if precise:
            self.precise.add(cat)

    def scored_strong(self) -> set[str]:
        """Categories worth +2: strong ones with a precise match, and weak ones with a precise match (a new set)."""
        return (self.strong & STRONG_CATEGORIES) | (self.precise & WEAK_CATEGORIES)

    def add_ticker(self, ticker: str) -> None:
        if len(self.tickers) < MAX_TICKERS and ticker not in self.tickers:
            self.tickers.append(ticker)

    def add_reason(self, reason: str) -> None:
        if reason not in self.reasons:
            self.reasons.append(reason)

    def ordered_categories(self) -> list[str]:
        return [c for c in CATEGORY_ORDER if c in self.categories]


def _snippet(text: str) -> str:
    s = " ".join(text.split())
    return s if len(s) <= _MAX_REASON_SNIPPET else s[: _MAX_REASON_SNIPPET - 1] + "…"


@dataclass(frozen=True)
class _CompiledKeyword:
    broad: tuple[Pattern[str], ...]
    precise: tuple[Pattern[str], ...]
    precise_defined: bool  # False: every broad match is precise


def _compile_keyword(spec: _KwSpec) -> _CompiledKeyword:
    broad_ci, broad_cs, precise_ci, precise_cs = spec
    broad = tuple(
        rx for rx in (
            re.compile(broad_ci, re.IGNORECASE) if broad_ci else None,
            re.compile(broad_cs) if broad_cs else None,
        ) if rx is not None
    )
    precise = tuple(
        rx for rx in (
            re.compile(precise_ci, re.IGNORECASE) if precise_ci else None,
            re.compile(precise_cs) if precise_cs else None,
        ) if rx is not None
    )
    return _CompiledKeyword(broad=broad, precise=precise, precise_defined=bool(precise))


_COMPILED_KEYWORDS: dict[str, _CompiledKeyword] = {cat: _compile_keyword(spec) for cat, spec in _KEYWORDS.items()}

# "positive topline results", "Phase 3 results", "results from the pivotal trial" are clinical
# results, not earnings: these phrases are blanked out of the text the BROAD earnings patterns see
# (the "results" hint). Precise earnings patterns see the original text, so "AbbVie Q3 results in
# clinical immunology up 23%" keeps its precise "q3 results". ("Robust topline results" is revenue.)
_CLINICAL_RESULTS_RE = re.compile(
    _alpha_words(
        r"(?:clinical|trial|study|pivotal|registrational|" + _PHASE + r")\s+(?:(?:trial|study)\s+)?"
        r"(?:results?|data|read-?outs?)",
        _OUTCOME_TOPLINE + r"\s+top-?line\s+(?:results?|data|read-?outs?)",
        r"results?\s+(?:from|of|in)\s+(?:(?:the|its|our|an?|two|both)\s+)?(?:[\w-]+\s+){0,2}?"
        r"(?:" + _PHASE + r"|clinical|pivotal|registrational|trials?|stud(?:y|ies))",
    ),
    re.IGNORECASE,
)

# A precise clinical / fda_action match only counts when it reports an event. These cues, in the
# same sentence just before the match, mark a question, a condition, a plan, a negation or a recap:
# "What happens to $APMD if the FDA issues a CRL?", "Apnimed expects topline results from the
# Phase 3 trial in Q2", "10 years ago today the FDA approved Trulicity", "Recall: ... met its
# primary endpoint". ("pleased to announce" / "as expected" / "once again" are not cues.)
_HEDGE_BEFORE_RE = re.compile(
    r"(?=[A-Za-z])"
    + _LB
    + r"(?:if|whether|unless|will|would|could|might|should|hypothetical(?:ly)?|imagine|assum(?:e|es|ing)"
    r"|(?<!as\s)(?<!than\s)expect(?:s|ed|ing)?|anticipat(?:e|es|ed|ing)|await(?:s|ing)?|ahead\s+of"
    r"|plan(?:s|ned|ning)?\s+to|hop(?:e|es|ed|ing)\s+to|aim(?:s|ed|ing)?\s+to"
    r"|(?<!pleased\s)(?<!proud\s)(?<!excited\s)(?<!happy\s)(?<!thrilled\s)(?<!delighted\s)(?<!glad\s)"
    r"to\s+(?:report|announce|present|release|share|unveil)"
    r"|on\s+track|upcoming|potential(?:ly)?|possibl[ey]|odds|chances?|likel(?:y|ihood)|unlikely"
    r"|predict(?:s|ed|ing|ions?)?|rumou?r(?:s|ed)?|reportedly|speculat(?:e|es|ed|ing|ion)"
    r"|no(?!\s+surprise)|not|never|without|[a-z]{1,8}n['’]t"
    r"|recall|remember(?:s|ed)?|ago|anniversary|previously|once(?!\s+again)"
    r"|last\s+(?:year|month|week|quarter|time)|back\s+in|in\s+(?:19|20)\d\d"
    r"|what\s+(?:if|happens|would|will|could|is|are)|why|how|when)"
    + r"(?![A-Za-z0-9_-])",
    re.IGNORECASE,
)
# ... and right after it: "topline results from the Phase 3 trial are expected in Q2", "met its
# primary endpoint back in 2025", "the FDA approved Trulicity 10 years ago".
_HEDGE_AFTER_RE = re.compile(
    r"\s*(?:(?:is|are|was|were|has\s+been|have\s+been|remains?|now)\s+)?(?:expected|anticipated|planned|due|pending"
    r"|possible|likely|unlikely)(?![A-Za-z0-9_])"
    r"|[^,;:]{0,40}?" + _LB + r"(?:back\s+in|(?:years?|months?|weeks?)\s+ago|ago|last\s+(?:year|month|week|quarter)"
    r"|previously|in\s+(?:19|20)\d\d)" + _RB,
    re.IGNORECASE,
)
_HEDGE_BEFORE_CHARS = 60
_HEDGE_AFTER_CHARS = 80
_SENTENCE_BREAK_BEFORE = ".!?;\n"
_CLAUSE_BREAK_AFTER_RE = re.compile(r"[.!\n,;:]")
#: at most this many matches per precise pattern are checked for hedges (bounded work per post)
_MAX_HEDGE_CHECKS = 8


def _hedged(text: str, start: int, end: int) -> bool:
    """True if the match ``text[start:end]`` is framed as a question, condition, plan, negation or recap."""
    before = text[max(0, start - _HEDGE_BEFORE_CHARS) : start]
    cut = max(before.rfind(c) for c in _SENTENCE_BREAK_BEFORE)
    if cut >= 0:
        before = before[cut + 1 :]
    if _HEDGE_BEFORE_RE.search(before):
        return True
    after = text[end : end + _HEDGE_AFTER_CHARS]
    if _HEDGE_AFTER_RE.match(after):
        return True
    m = _CLAUSE_BREAK_AFTER_RE.search(after)
    return "?" in (after[: m.start()] if m else after)


def _blank(rx: Pattern[str], text: str) -> str:
    """``text`` with every match of ``rx`` replaced by spaces (same length; no copy if none)."""
    if rx.search(text) is None:
        return text
    return rx.sub(lambda m: " " * (m.end() - m.start()), text)


def _alias_pattern(alias: str) -> str:
    parts = [re.escape(p) for p in alias.split()]
    return r"\s+".join(parts)


def _norm_space(s: str) -> str:
    return " ".join(s.split())


def _at_sentence_start(text: str, start: int) -> bool:
    i = start
    while i > 0 and text[i - 1] in _SENTENCE_LEAD_CHARS:
        i -= 1
    return i == 0 or text[i - 1] in _SENTENCE_END_CHARS


def _capped(text: str | None, limit: int) -> str:
    if not text:
        return ""
    return text if len(text) <= limit else text[:limit]


def own_text(post: Post) -> str:
    """The author's own words: the post text, plus the original of a pure repost (capped)."""
    parts = [_capped(post.text, MAX_POST_CHARS)]
    parts.extend(
        _capped(r.text, MAX_REFERENCED_CHARS)
        for r in post.referenced[:MAX_REFERENCED_POSTS]
        if r.type == "retweeted" and r.text
    )
    return "\n".join(p for p in parts if p) if len(parts) > 1 else parts[0]


def combined_text(post: Post) -> str:
    """Post text plus the text of every referenced (quoted / parent / reposted) post (capped)."""
    parts = [_capped(post.text, MAX_POST_CHARS)]
    parts.extend(_capped(r.text, MAX_REFERENCED_CHARS) for r in post.referenced[:MAX_REFERENCED_POSTS] if r.text)
    return "\n".join(parts)


class FastRules:
    """Regex pre-filter. Construct once per watchlist; `evaluate` is pure and thread-safe."""

    def __init__(self, watchlist: Watchlist) -> None:
        self.watchlist = watchlist
        self.my_tickers: tuple[str, ...] = tuple(t.upper() for t in watchlist.my_tickers)

        # Bare uppercase tickers: only >= 3 chars (letters part), case-sensitive, and not an everyday word.
        # 1-2 character tickers ("MU", "F") and COMMON_WORD_TICKERS ("NOW", "APP") only count as $cashtags.
        bare = sorted(
            {
                t
                for t in self.my_tickers
                if len(t.replace(".", "")) >= 3 and _TICKER_SHAPE.match(t) and t not in COMMON_WORD_TICKERS
            },
            key=len,
            reverse=True,
        )
        self._bare_ticker_re: Pattern[str] | None = (
            re.compile(r"(?<![A-Za-z0-9_$])(" + "|".join(re.escape(t) for t in bare) + r")(?![A-Za-z0-9_])")
            if bare
            else None
        )

        # Company names / aliases -> tickers. Case-insensitive by default; common English words
        # (COMMON_WORD_ALIASES) and aliases written as "=Name" match with exact case only.
        ci_map: dict[str, list[str]] = {}  # lowercased alias -> tickers
        ci_display: dict[str, str] = {}
        cs_map: dict[str, list[str]] = {}  # exact alias -> tickers
        for ticker, names in watchlist.company_names.items():
            for raw_name in names:
                name = str(raw_name).strip()
                exact = name.startswith(_EXACT_CASE_MARK)
                name = _norm_space(name.lstrip(_EXACT_CASE_MARK))
                if not name:
                    continue
                lower = name.lower()
                if exact or lower in COMMON_WORD_ALIASES:
                    lst = cs_map.setdefault(name, [])
                else:
                    ci_display.setdefault(lower, name)
                    lst = ci_map.setdefault(lower, [])
                if ticker.upper() not in lst:
                    lst.append(ticker.upper())
        self._ci_alias_to_tickers = ci_map
        self._ci_alias_display = ci_display
        self._cs_alias_to_tickers = cs_map
        self._company_ci_re = self._alias_regex(list(ci_display.values()), re.IGNORECASE)
        self._company_cs_re = self._alias_regex(list(cs_map), 0)

    @staticmethod
    def _alias_regex(aliases: list[str], flags: int) -> Pattern[str] | None:
        if not aliases:
            return None
        ordered = sorted(aliases, key=len, reverse=True)  # longest first: "Model Y" before "Y"
        return re.compile(_LB + "(" + "|".join(_alias_pattern(a) for a in ordered) + ")" + _RB, flags)

    # ------------------------------------------------------------------ public API

    def evaluate(self, post: Post, account: Account | None) -> RuleResult:
        """Flag posts that mention tickers, companies, market-moving keywords or big numbers.

        ``categories`` / ``tickers`` / ``rule_score`` cover the post plus its quoted / parent /
        reposted posts; ``own_*`` cover only the author's own words (see `own_text`).
        """
        own = self._scan(own_text(post), post)
        has_context = any(r.text for r in post.referenced[:MAX_REFERENCED_POSTS] if r.type != "retweeted")
        full = self._scan(combined_text(post), post) if has_context else own

        # A watched account posting a strong own-text category ("we acquired 1,000 BTC", "I'm
        # stepping down") is about its own company: count its mapped tickers as a ticker hit.
        # (A precise weak category - "commercial launch", "$1B upfront" - does not: brand news.)
        account_hit = bool(account is not None and account.tickers and (own.strong & STRONG_CATEGORIES))

        own_categories = own.ordered_categories()
        own_score = self._rule_score(own, account, account_hit) if own_categories else 0
        categories = full.ordered_categories()
        flagged = bool(categories)
        score = self._rule_score(full, account, account_hit) if flagged else 0
        result = RuleResult(
            flagged=flagged,
            reasons=full.reasons,
            categories=categories,
            tickers=full.tickers,
            has_numbers=full.has_numbers,
            rule_score=score,
            own_flagged=bool(own_categories),
            own_categories=own_categories,
            own_rule_score=own_score,
            own_tickers=list(own.tickers),
        )
        logger.debug(
            "fast rules post=%s flagged=%s categories=%s tickers=%s score=%d own=%s/%d",
            post.id,
            flagged,
            categories,
            full.tickers,
            score,
            own_categories,
            own_score,
        )
        return result

    # ------------------------------------------------------------------ scanners

    def _scan(self, text: str, post: Post) -> _Scan:
        scan = _Scan()
        self._scan_cashtags(text, post, scan)
        self._scan_bare_tickers(text, scan)
        self._scan_companies(text, scan)
        masked = self._scan_bitcoin(text, scan)
        self._scan_keywords(masked, scan)
        scan.has_numbers = self._scan_numbers(text, scan)
        return scan

    def _scan_cashtags(self, text: str, post: Post, scan: _Scan) -> None:
        found: list[str] = []
        for m in _CASHTAG_RE.finditer(text):
            found.append(m.group(1).upper())
        for tag in post.cashtags:  # X entities (post text only); keep regex order first
            t = str(tag).strip().lstrip("$").upper()
            if t:
                found.append(t)
        n_reasons = 0
        for t in dict.fromkeys(found):
            if not _TICKER_SHAPE.match(t):
                continue
            scan.add_category(CAT_TICKER)
            scan.add_ticker(t)
            if n_reasons < _MAX_TICKER_REASONS:
                scan.add_reason(f"cashtag ${t}")
                n_reasons += 1

    def _scan_bare_tickers(self, text: str, scan: _Scan) -> None:
        if self._bare_ticker_re is None:
            return
        for m in self._bare_ticker_re.finditer(text):
            t = m.group(1)
            scan.add_category(CAT_TICKER)
            if t not in scan.tickers:
                scan.add_ticker(t)
                scan.add_reason(f"ticker {t}")

    def _scan_companies(self, text: str, scan: _Scan) -> None:
        hits: list[tuple[int, int, str, list[str]]] = []
        if self._company_ci_re is not None:
            for m in self._company_ci_re.finditer(text):
                key = _norm_space(m.group(1)).lower()
                tickers = self._ci_alias_to_tickers.get(key)
                if tickers:
                    hits.append((m.start(), m.end(), self._ci_alias_display[key], tickers))
        if self._company_cs_re is not None:
            ci_spans = [(s, e) for s, e, _, _ in hits]
            for m in self._company_cs_re.finditer(text):
                if any(s < m.end() and m.start() < e for s, e in ci_spans):
                    continue  # part of a longer case-insensitive alias ("Meta Quest")
                key = _norm_space(m.group(1))
                tickers = self._cs_alias_to_tickers.get(key)
                if not tickers:  # pragma: no cover - regex built from the same keys
                    continue
                if key.lower() in MID_SENTENCE_ONLY_ALIASES and _at_sentence_start(text, m.start()):
                    continue
                hits.append((m.start(), m.end(), key, tickers))
        for _, _, display, tickers in sorted(hits):
            scan.add_category(CAT_COMPANY)
            scan.add_reason(f"company: {display}")
            for t in tickers:
                scan.add_ticker(t)

    def _scan_bitcoin(self, text: str, scan: _Scan) -> str:
        """Detect bitcoin purchases and sales; returns the text with purchase phrases masked.

        Masking keeps "acquired 1,000 BTC" from also counting as an M&A acquisition.
        """
        masked = text
        hits: list[str] = []
        for m in _BITCOIN_PURCHASE_RE.finditer(text):
            hits.append(m.group(0))
            masked = masked[: m.start()] + " " * (m.end() - m.start()) + masked[m.end() :]
        sales: list[str] = []
        for m in _BITCOIN_SALE_RE.finditer(text):
            if _NEGATION_BEFORE_RE.search(text[max(0, m.start() - 40) : m.start()]):
                continue  # "never sell your bitcoin"
            sales.append(m.group(0))
        for m in _BITCOIN_AMOUNT_RE.finditer(text):
            hits.append(m.group(0))
        if not hits and not sales and _BITCOIN_SYMBOL_RE.search(text):
            hits.append("₿")
        if sales:
            scan.add_category(CAT_BITCOIN)
            scan.add_reason(f"bitcoin sale: {_snippet(sales[0])}")
        elif hits:
            scan.add_category(CAT_BITCOIN)
            scan.add_reason(f"bitcoin: {_snippet(hits[0])}")
        return masked

    def _scan_keywords(self, text: str, scan: _Scan) -> None:
        # "positive topline results" is not earnings: the broad "results" hint skips clinical results
        earnings_broad_text = _blank(_CLINICAL_RESULTS_RE, text)
        for cat, kw in _COMPILED_KEYWORDS.items():
            hedged_word = None
            if cat in BINARY_CATALYST_CATEGORIES:
                # "if the FDA issues a CRL?" / "met its primary endpoint last year": a hint, not an event
                precise_word, hedged_word = self._first_unhedged(kw.precise, text)
            else:
                precise_word = self._first_match(kw.precise, text)
            broad_text = earnings_broad_text if cat == CAT_EARNINGS else text
            word = precise_word or self._first_match(kw.broad, broad_text) or hedged_word
            if word:
                strong = precise_word is not None or not kw.precise_defined
                scan.add_category(cat, strong=strong, precise=precise_word is not None)
                scan.add_reason(f"keyword: {word}")

    @staticmethod
    def _word(rx: Pattern[str], m: re.Match[str]) -> str:
        word = _norm_space(m.group(0))
        # acronyms ("SEC", "EPS", "CEO") keep their case; everything else lowercase
        return word.lower() if (rx.flags & re.IGNORECASE) else word

    @classmethod
    def _first_match(cls, patterns: tuple[Pattern[str], ...], text: str) -> str | None:
        for rx in patterns:
            m = rx.search(text)
            if m:
                return cls._word(rx, m)
        return None

    @classmethod
    def _first_unhedged(cls, patterns: tuple[Pattern[str], ...], text: str) -> tuple[str | None, str | None]:
        """(first match not framed as a question / plan / recap, first hedged match) - either may be None."""
        hedged: str | None = None
        for rx in patterns:
            for m in itertools.islice(rx.finditer(text), _MAX_HEDGE_CHECKS):
                if not _hedged(text, m.start(), m.end()):
                    return cls._word(rx, m), hedged
                if hedged is None:
                    hedged = cls._word(rx, m)
        return None, hedged

    def _scan_numbers(self, text: str, scan: _Scan) -> bool:
        spans: list[tuple[int, int, str]] = []
        for rx in _NUMBER_RES:
            # Only the first few matches per pattern matter (3 reasons + a flag); without this
            # bound the overlap check below is quadratic on "1%1%1%..." (0.5 s for 8k chars).
            for m in itertools.islice(rx.finditer(text), _MAX_NUMBER_MATCHES_PER_PATTERN):
                value = _norm_space(m.group(0))
                if _AGREEMENT_PERCENT_RE.match(value):
                    continue
                if any(s <= m.start() < e or m.start() <= s < m.end() for s, e, _ in spans):
                    continue  # overlapping match from another pattern
                spans.append((m.start(), m.end(), value))
        if not spans:
            return False
        scan.add_category(CAT_NUMBERS)
        for _, _, value in sorted(spans)[:_MAX_NUMBER_REASONS]:
            scan.add_reason(f"number: {_snippet(value)}")
        return True

    # ------------------------------------------------------------------ scoring

    def _rule_score(self, scan: _Scan, account: Account | None, account_hit: bool) -> int:
        """Rough 1-10 heuristic, used only when Claude is unavailable.

        +3 ticker / company mention (or a watched account's own strong news), +2 per strong
        category with a precise match, +1 per weak category (+2 with a precise match), +1 once
        for a binary biotech catalyst (precise clinical / fda_action) in a news post or from a
        company account, +1 numbers, +1 if a detected ticker is one of mine / the account's.
        With a binary catalyst, regulatory news does not count again: "FDA approves X" is one
        event, not an FDA mention plus an approval.
        """
        categories = scan.categories
        scored = scan.scored_strong()
        binary = bool(scored & BINARY_CATALYST_CATEGORIES)
        if binary:
            scored.discard(CAT_REGULATORY)
        score = 0
        if CAT_TICKER in categories or CAT_COMPANY in categories or account_hit:
            score += 3
        score += 2 * sum(1 for c in categories if c in scored)
        score += sum(1 for c in categories if c in WEAK_CATEGORIES and c not in scored)
        if binary and (account is None or account.kind == "company"):
            score += BINARY_CATALYST_BONUS
        if CAT_NUMBERS in categories:
            score += 1
        tickers = set(scan.tickers)
        if account_hit and account is not None:
            tickers.update(account.tickers)
        relevant = set(self.my_tickers) | set(account.tickers if account else ())
        if tickers and relevant.intersection(tickers):
            score += 1
        return max(1, min(10, score))
