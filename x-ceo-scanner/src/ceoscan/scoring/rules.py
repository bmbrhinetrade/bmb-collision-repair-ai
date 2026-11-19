"""Stage 1 scoring: fast, deterministic regex rules.

`FastRules.evaluate` decides in microseconds whether a post is worth sending to Claude.
It is deliberately recall-oriented (a false positive only costs one cheap Claude call),
but it must stay quiet on plain chit-chat ("Good morning!", "lol so true", "100%").

The rules look at the post text plus the text of any quoted / replied-to / reposted
post, because "Yes" in reply to "Will Tesla raise guidance?" is market relevant.
"""

from __future__ import annotations

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
CAT_M_AND_A = "m_and_a"
CAT_PARTNERSHIP = "partnership"
CAT_LAYOFFS = "layoffs"
CAT_PRODUCT = "product"
CAT_REGULATORY = "regulatory"
CAT_BUYBACK = "buyback"
CAT_OFFERING = "offering"
CAT_BITCOIN = "bitcoin"
CAT_NUMBERS = "numbers"

CATEGORY_ORDER: tuple[str, ...] = (
    CAT_TICKER,
    CAT_COMPANY,
    CAT_EARNINGS,
    CAT_GUIDANCE,
    CAT_M_AND_A,
    CAT_PARTNERSHIP,
    CAT_LAYOFFS,
    CAT_PRODUCT,
    CAT_REGULATORY,
    CAT_BUYBACK,
    CAT_OFFERING,
    CAT_BITCOIN,
    CAT_NUMBERS,
)

# Categories that on their own usually mean "material company news".
STRONG_CATEGORIES: frozenset[str] = frozenset(
    {
        CAT_EARNINGS,
        CAT_GUIDANCE,
        CAT_M_AND_A,
        CAT_LAYOFFS,
        CAT_REGULATORY,
        CAT_BUYBACK,
        CAT_OFFERING,
        CAT_BITCOIN,
    }
)
# Categories that are often marketing / brand noise: worth a look, weaker signal.
WEAK_CATEGORIES: frozenset[str] = frozenset({CAT_PARTNERSHIP, CAT_PRODUCT})

_TICKER_SHAPE = re.compile(r"^[A-Z][A-Z0-9.]{0,9}$")

# $TSLA, $tsla, $BRK.B ; never "$5B" (must start with a letter). "$TSLA." -> TSLA.
_CASHTAG_RE = re.compile(r"(?<![A-Za-z0-9_$])\$([A-Za-z]{1,6}(?:\.[A-Za-z]{1,2})?)(?![A-Za-z0-9_])")

# Word boundary that also treats digits/underscore as word characters but works for
# aliases that themselves start/end with digits ("18A", "GB200", "Falcon 9").
_LB = r"(?<![A-Za-z0-9_])"
_RB = r"(?![A-Za-z0-9_])"


def _words(*alternatives: str) -> str:
    return _LB + "(?:" + "|".join(alternatives) + ")" + _RB


# Keyword categories. Each entry: (case-insensitive pattern | None, case-sensitive pattern | None).
# Acronyms are case-sensitive so "atm" (at the moment) or "sec" (second) stay chit-chat.
_KEYWORDS: dict[str, tuple[str | None, str | None]] = {
    CAT_EARNINGS: (
        _words(r"earnings", r"results", r"quarter", r"quarterly", r"Q[1-4]", r"fiscal\s+year", r"beat\s+estimates"),
        _words(r"EPS"),
    ),
    CAT_GUIDANCE: (
        _words(r"guidance", r"outlook", r"forecasts?", r"forecasting", r"forecasted"),
        None,
    ),
    CAT_M_AND_A: (
        _words(
            r"acquisitions?",
            r"acquir(?:e|es|ed|ing)",
            r"mergers?",
            r"merg(?:e|es|ed|ing)\s+with",
            r"buy-?outs?",
            r"takeovers?",
            r"take\s+private",
            r"tender\s+offer",
        ),
        _words(r"M&A"),
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
    ),
    CAT_PRODUCT: (
        _words(
            r"launch(?:es|ed|ing)?",
            r"unveil(?:s|ed|ing)?",
            r"announc(?:e|es|ed|ing|ement|ements)",
            r"releas(?:e|es|ed|ing)",
            r"available\s+now",
            r"now\s+available",
            r"ship(?:s|ped|ping)?",
            r"pre-?orders?",
            r"rolling\s+out",
            r"roll(?:s|ed)?\s+out",
        ),
        None,
    ),
    CAT_REGULATORY: (
        _words(
            r"antitrust",
            r"regulators?",
            r"regulatory",
            r"approval",
            r"approv(?:ed|es)",
            r"investigations?",
            r"investigat(?:e|es|ed|ing)",
            r"probe",
            r"lawsuits?",
            r"su(?:es|ed|ing)",
            r"subpoena(?:s|ed)?",
            r"ban(?:s|ned)?",
            r"tariffs?",
        ),
        _words(r"FDA", r"SEC", r"FTC", r"DOJ", r"CFTC", r"NHTSA", r"FCC", r"EU\s+Commission"),
    ),
    CAT_BUYBACK: (
        _words(r"buy-?backs?", r"buy(?:ing)?\s+back", r"repurchas(?:e|es|ed|ing)"),
        None,
    ),
    CAT_OFFERING: (
        _words(
            r"offerings?",
            # "raise" only in the capital-raising sense: not "raise guidance" / "raise prices"
            r"rais(?:e|es|ed|ing)\s+(?:~|about\s+|over\s+|up\s+to\s+)?"
            r"(?:\$?\s?\d[\d,.]*\s?[A-Za-z]*|capital|funds|money|cash|equity|debt|a\s+round)",
            r"(?:capital|equity)\s+raises?",
            r"fundrais(?:e|es|ed|ing)",
            r"convertibles?",
            r"notes",
            r"dilution",
            r"dilutive",
            r"at-the-market",
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
# "1,000 BTC", "₿ 21,000", "21K bitcoin"
_BITCOIN_AMOUNT_RE = re.compile(
    r"(?:" + _LB + r"\d[\d,]*(?:\.\d+)?\s*(?:[KkMm]\b)?\s*(?:BTC|bitcoins?)" + _RB + r")"
    + r"|(?:₿\s*\d[\d,]*(?:\.\d+)?)"
    + r"|(?:\d[\d,]*(?:\.\d+)?\s*₿)",
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
        _LB + r"\d[\d,]*(?:\.\d+)?\s?" + _MAGNITUDE + r"?\s+(?:" + _UNIT_WORDS + r")" + _RB,
        re.IGNORECASE,
    ),
    # 10 GW, 1.5 GWh, 50 EH/s
    re.compile(_LB + r"\d[\d,]*(?:\.\d+)?\s?(?:GW|MW|TW|GWh|MWh|TWh|kWh|EH/s)" + _RB),
    # 500 million, 2 billion
    re.compile(_LB + r"\d[\d,]*(?:\.\d+)?\s?(?:million|billion|trillion)" + _RB, re.IGNORECASE),
)
_AGREEMENT_PERCENT_RE = re.compile(r"^100\s?%$")

_MAX_TICKER_REASONS = 6
_MAX_NUMBER_REASONS = 3
_MAX_REASON_SNIPPET = 60


@dataclass
class _Scan:
    """Mutable accumulator for one evaluation."""

    reasons: list[str] = field(default_factory=list)
    categories: list[str] = field(default_factory=list)
    tickers: list[str] = field(default_factory=list)

    def add_category(self, cat: str) -> None:
        if cat not in self.categories:
            self.categories.append(cat)

    def add_ticker(self, ticker: str) -> None:
        if ticker not in self.tickers:
            self.tickers.append(ticker)

    def add_reason(self, reason: str) -> None:
        if reason not in self.reasons:
            self.reasons.append(reason)


def _snippet(text: str) -> str:
    s = " ".join(text.split())
    return s if len(s) <= _MAX_REASON_SNIPPET else s[: _MAX_REASON_SNIPPET - 1] + "…"


def _compile_keyword(ci: str | None, cs: str | None) -> tuple[Pattern[str] | None, Pattern[str] | None]:
    return (re.compile(ci, re.IGNORECASE) if ci else None, re.compile(cs) if cs else None)


_COMPILED_KEYWORDS: dict[str, tuple[Pattern[str] | None, Pattern[str] | None]] = {
    cat: _compile_keyword(ci, cs) for cat, (ci, cs) in _KEYWORDS.items()
}


def _alias_pattern(alias: str) -> str:
    parts = [re.escape(p) for p in alias.split()]
    return r"\s+".join(parts)


def combined_text(post: Post) -> str:
    """Post text plus the text of every referenced (quoted / parent / reposted) post."""
    parts = [post.text or ""]
    parts.extend(r.text for r in post.referenced if r.text)
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

        # Company names / aliases -> ticker, case-insensitive, word boundaries.
        alias_to_tickers: dict[str, list[str]] = {}
        alias_display: dict[str, str] = {}
        for ticker, names in watchlist.company_names.items():
            for name in names:
                name = str(name).strip()
                if not name:
                    continue
                key = " ".join(name.lower().split())
                alias_display.setdefault(key, name)
                lst = alias_to_tickers.setdefault(key, [])
                if ticker.upper() not in lst:
                    lst.append(ticker.upper())
        self._alias_to_tickers = alias_to_tickers
        self._alias_display = alias_display
        aliases = sorted(alias_display.values(), key=len, reverse=True)  # longest first: "Model Y" before "Y"
        self._company_re: Pattern[str] | None = (
            re.compile(_LB + "(" + "|".join(_alias_pattern(a) for a in aliases) + ")" + _RB, re.IGNORECASE)
            if aliases
            else None
        )

    # ------------------------------------------------------------------ public API

    def evaluate(self, post: Post, account: Account | None) -> RuleResult:
        """Flag posts that mention tickers, companies, market-moving keywords or big numbers."""
        text = combined_text(post)
        scan = _Scan()

        self._scan_cashtags(text, post, scan)
        self._scan_bare_tickers(text, scan)
        self._scan_companies(text, scan)
        masked = self._scan_bitcoin(text, scan)
        self._scan_keywords(masked, scan)
        has_numbers = self._scan_numbers(text, scan)

        categories = [c for c in CATEGORY_ORDER if c in scan.categories]
        flagged = bool(categories)
        score = self._rule_score(categories, scan.tickers, account) if flagged else 0
        result = RuleResult(
            flagged=flagged,
            reasons=scan.reasons,
            categories=categories,
            tickers=scan.tickers,
            has_numbers=has_numbers,
            rule_score=score,
        )
        logger.debug(
            "fast rules post=%s flagged=%s categories=%s tickers=%s score=%d",
            post.id,
            flagged,
            categories,
            scan.tickers,
            score,
        )
        return result

    # ------------------------------------------------------------------ scanners

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
        if self._company_re is None:
            return
        for m in self._company_re.finditer(text):
            key = " ".join(m.group(1).lower().split())
            tickers = self._alias_to_tickers.get(key)
            if not tickers:  # pragma: no cover - regex built from the same keys
                continue
            scan.add_category(CAT_COMPANY)
            scan.add_reason(f"company: {self._alias_display[key]}")
            for t in tickers:
                scan.add_ticker(t)

    def _scan_bitcoin(self, text: str, scan: _Scan) -> str:
        """Detect bitcoin purchases; returns the text with purchase phrases masked.

        Masking keeps "acquired 1,000 BTC" from also counting as an M&A acquisition.
        """
        masked = text
        hits: list[str] = []
        for m in _BITCOIN_PURCHASE_RE.finditer(text):
            hits.append(m.group(0))
            masked = masked[: m.start()] + " " * (m.end() - m.start()) + masked[m.end() :]
        for m in _BITCOIN_AMOUNT_RE.finditer(text):
            hits.append(m.group(0))
        if not hits and _BITCOIN_SYMBOL_RE.search(text):
            hits.append("₿")
        if hits:
            scan.add_category(CAT_BITCOIN)
            scan.add_reason(f"bitcoin: {_snippet(hits[0])}")
        return masked

    def _scan_keywords(self, text: str, scan: _Scan) -> None:
        for cat, (ci, cs) in _COMPILED_KEYWORDS.items():
            word: str | None = None
            if ci is not None and (m := ci.search(text)):
                word = " ".join(m.group(0).split()).lower()
            elif cs is not None and (m := cs.search(text)):
                word = " ".join(m.group(0).split())  # acronyms keep their case: "SEC"
            if word:
                scan.add_category(cat)
                scan.add_reason(f"keyword: {word}")

    def _scan_numbers(self, text: str, scan: _Scan) -> bool:
        spans: list[tuple[int, int, str]] = []
        for rx in _NUMBER_RES:
            for m in rx.finditer(text):
                value = " ".join(m.group(0).split())
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

    def _rule_score(self, categories: list[str], tickers: list[str], account: Account | None) -> int:
        """Rough 0-10 heuristic used only when Claude is unavailable."""
        score = 0
        if CAT_TICKER in categories or CAT_COMPANY in categories:
            score += 3
        score += 2 * sum(1 for c in categories if c in STRONG_CATEGORIES)
        score += sum(1 for c in categories if c in WEAK_CATEGORIES)
        if CAT_NUMBERS in categories:
            score += 1
        relevant = set(self.my_tickers) | set(account.tickers if account else ())
        if tickers and relevant.intersection(tickers):
            score += 1
        return max(1, min(10, score))
