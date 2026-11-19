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
        CAT_CORPORATE_STATUS,
        CAT_INCIDENT,
        CAT_BUYBACK,
        CAT_CAPITAL_RETURN,
        CAT_OFFERING,
        CAT_BITCOIN,
    }
)
# Categories that are often marketing / brand noise: worth a look, weaker signal (+1).
WEAK_CATEGORIES: frozenset[str] = frozenset({CAT_PARTNERSHIP, CAT_PRODUCT})

#: Only this much text is scanned: the post itself, and each quoted / parent / reposted post.
#: (X posts can be 25,000 characters; Claude still sees the full text.)
MAX_POST_CHARS = 8_000
MAX_REFERENCED_CHARS = 4_000
MAX_REFERENCED_POSTS = 4
#: at most this many detected tickers are kept (a post listing 1,000 cashtags is spam)
MAX_TICKERS = 25

_TICKER_SHAPE = re.compile(r"^[A-Z][A-Z0-9.]{0,9}$")

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
        ),
        None,
        None,
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
        None,
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
    CAT_CORPORATE_STATUS: (
        _words(r"licen[cs](?:e|es|ed|ing)", r"charter"),
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
            r"(?:[\w-]+\s+){0,2}?" + _LICENSE,
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
    strong: set[str] = field(default_factory=set)  # categories with a precise match
    tickers: list[str] = field(default_factory=list)
    has_numbers: bool = False

    def add_category(self, cat: str, strong: bool = True) -> None:
        if cat not in self.categories:
            self.categories.append(cat)
        if strong:
            self.strong.add(cat)

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

        # Bare uppercase tickers: only >= 3 chars (letters part), case-sensitive.
        # 1-2 character tickers ("MU", "F") are far too ambiguous and only count as $cashtags.
        bare = sorted(
            {t for t in self.my_tickers if len(t.replace(".", "")) >= 3 and _TICKER_SHAPE.match(t)},
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
        for cat, kw in _COMPILED_KEYWORDS.items():
            precise_word = self._first_match(kw.precise, text)
            word = precise_word or self._first_match(kw.broad, text)
            if word:
                strong = precise_word is not None or not kw.precise_defined
                scan.add_category(cat, strong=strong)
                scan.add_reason(f"keyword: {word}")

    @staticmethod
    def _first_match(patterns: tuple[Pattern[str], ...], text: str) -> str | None:
        for rx in patterns:
            m = rx.search(text)
            if m:
                word = _norm_space(m.group(0))
                # acronyms ("SEC", "EPS", "CEO") keep their case; everything else lowercase
                return word.lower() if (rx.flags & re.IGNORECASE) else word
        return None

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
        category with a precise match, +1 per weak category, +1 numbers, +1 if a detected
        ticker is one of mine / the account's.
        """
        categories = scan.categories
        score = 0
        if CAT_TICKER in categories or CAT_COMPANY in categories or account_hit:
            score += 3
        score += 2 * sum(1 for c in categories if c in STRONG_CATEGORIES and c in scan.strong)
        score += sum(1 for c in categories if c in WEAK_CATEGORIES)
        if CAT_NUMBERS in categories:
            score += 1
        tickers = set(scan.tickers)
        if account_hit and account is not None:
            tickers.update(account.tickers)
        relevant = set(self.my_tickers) | set(account.tickers if account else ())
        if tickers and relevant.intersection(tickers):
            score += 1
        return max(1, min(10, score))
