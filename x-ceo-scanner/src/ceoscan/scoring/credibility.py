"""Who posted it, and how believable is the source? Computed in code from X account metadata.

:func:`assess_credibility` returns a :class:`~ceoscan.models.Credibility` with one of five levels:

* **official** - a watched company / CEO account (the watchlist). If that account posts scam-like crypto
  content (:func:`compromise_check`: wallet addresses, memecoin trading links, look-alike link domains, "claim now"
  / airdrop / presale hype) it is **suspicious** instead, with ``possible_compromise`` set: the account may be
  hijacked (the Jul 2026 @vladtenev memecoin post). A check that is likely but not definitive is confirmed with
  Claude by the pipeline (see :func:`without_compromise`).
* **trusted** - a listed trusted ``sources`` entry (regulator, wire; ``is_trusted_source``) or a business /
  government-verified account with at least ``red_flags.trusted_min_followers`` followers. A link never raises the
  level: anyone can link to reuters.com's front page, an old article or a paid press-release wire, so a link to a
  ``red_flags.trusted_domains`` site is only a reason ("links to reuters.com (trusted domain)") and a hint for Claude.
* **established** - blue / business-verified with at least ``red_flags.established_min_followers`` followers and
  an account older than a year. Listed sources (short sellers, journalists) are at least established.
* **suspicious** - an account younger than ``red_flags.suspicious_account_age_days``, an X parody label, or a
  possible impersonation: the display name or handle imitates a watched PERSON's name, or is exactly a watched
  company's name, while not being that account (business / government-verified accounts are never flagged: X
  verifies organisations and their affiliates). A handle only imitates when the extra characters are decoration
  (elonmusk_ceo, real_elonmusk, elonmusk2), never another word (MarketIntel, MacroStrategy), and an old account with
  a large following is never flagged on its handle alone.
* **unverified** - everything else.

``reasons`` are short human-readable facts ("business-verified", "2.1M followers", "account created 12 days ago",
"links to reuters.com (trusted domain)"). They never contain post text or display names (both are written by the
author, i.e. untrusted), only X metadata and validated handles / domains, so they can go into the trusted part of
the Claude prompt.

:func:`clamp_claim` cross-checks a score's claim status (Claude's reading of untrusted text) against this
credibility: only the watched account itself, about its own tickers, can be CONFIRMED (official); only official or
trusted authors can be CREDIBLE; a possible impersonator is LIKELY FALSE.
"""

from __future__ import annotations

import re
import unicodedata
from datetime import UTC
from urllib.parse import urlsplit

from ..config import RedFlagSettings
from ..models import Account, ClaimStatus, Credibility, Post, Score, Source, Watchlist
from .redflags import CompromiseCheck, brand_tokens, detect_compromise
from .rules import own_text

#: at most this many link domains are kept per post
MAX_LINK_DOMAINS = 5
#: an account must be at least this old (days) to count as established
ESTABLISHED_MIN_AGE_DAYS = 365
#: X verified_type values (anything else is shown as "verified (type unknown)" when author_verified is true)
VERIFIED_TYPES = ("blue", "business", "government")

_HANDLE_RE = re.compile(r"^[A-Za-z0-9_]{1,15}$")
_LABEL = r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?"
_DOMAIN_RE = re.compile(rf"^(?=.{{3,253}}$){_LABEL}(?:\.{_LABEL})+$")
#: display-name decorations ignored when comparing with a watched company's name
_NAME_NOISE = frozenset({"official", "real", "the", "inc", "corp", "corporation", "co", "hq", "llc", "plc", "ltd"})
# look-alike characters in handles: tesIa / te5la / elonmusk1 / elonrnusk
_HANDLE_HOMOGLYPHS = str.maketrans({"0": "o", "1": "l", "i": "l", "5": "s", "3": "e", "4": "a", "7": "t", "8": "b"})
#: a handle containing a watched handle plus at most this many extra characters imitates it (elonmusk_ceo)
_HANDLE_EXTRA_CHARS = 6
#: watched handles shorter than this are compared exactly (after look-alike folding), never by containment
_HANDLE_CONTAIN_MIN = 5
#: the only extra words a handle may add to a watched handle and still imitate it (elonmusk_ceo, therealelonmusk)
_HANDLE_DECORATION = frozenset(
    {"real", "the", "official", "offical", "ceo", "hq", "inc", "news", "its", "iam", "im", "x", "og", "team",
     "support", "help", "verified", "corp", "co", "app", "updates", "update", "live", "us", "usa"}
)
#: an account at least this old (days) with established_min_followers is never flagged on its handle alone
_HANDLE_OLD_DAYS = 730


def safe_handle(handle: str | None) -> str | None:
    """``handle`` when it is a valid X username (letters, digits, underscore; 1-15 characters), else None."""
    h = (handle or "").strip().lstrip("@")
    return h if _HANDLE_RE.match(h) else None


def link_domain(url: str | None) -> str | None:
    """Lowercase host of an (expanded) URL without "www.", or None when it is not a plain domain name."""
    if not url or not isinstance(url, str):
        return None
    candidate = url.strip()
    if "://" not in candidate:
        candidate = "https://" + candidate
    try:
        host = urlsplit(candidate).hostname
    except ValueError:
        return None
    if not host:
        return None
    host = host.lower().rstrip(".")
    if host.startswith("www."):
        host = host[4:]
    return host if _DOMAIN_RE.match(host) else None


def link_domains(urls: list[str] | tuple[str, ...]) -> list[str]:
    """Distinct link domains of a post's expanded URLs (validated, at most MAX_LINK_DOMAINS)."""
    out: list[str] = []
    for u in urls:
        d = link_domain(u)
        if d and d not in out:
            out.append(d)
        if len(out) >= MAX_LINK_DOMAINS:
            break
    return out


def is_trusted_domain(domain: str, trusted: list[str] | tuple[str, ...]) -> bool:
    """``reuters.com`` and ``www.reuters.com`` / ``uk.reuters.com`` match the trusted entry ``reuters.com``."""
    d = domain.lower()
    for t in trusted:
        t = str(t).strip().lower()
        if t and (d == t or d.endswith("." + t)):
            return True
    return False


def format_count(n: int | None) -> str:
    """2.1M / 312K / 850 followers."""
    if n is None:
        return "?"
    if n >= 1_000_000:
        return f"{n / 1_000_000:.1f}M".replace(".0M", "M")
    if n >= 10_000:
        return f"{n // 1000}K"
    if n >= 1_000:
        return f"{n / 1000:.1f}K".replace(".0K", "K")
    return str(n)


def account_age_days(post: Post) -> int | None:
    """Days between the account's creation and the post, or None when X did not send created_at."""
    created = post.author_created_at
    if created is None:
        return None
    c = created if created.tzinfo else created.replace(tzinfo=UTC)
    p = post.created_at if post.created_at.tzinfo else post.created_at.replace(tzinfo=UTC)
    return max(0, int((p - c).total_seconds() // 86_400))


def format_age_days(days: int) -> str:
    if days < 60:
        return f"account created {days} day{'s' if days != 1 else ''} ago"
    if days < 730:
        return f"account {days // 30} months old"
    return f"account {days // 365} years old"


def verified_type(post: Post) -> str | None:
    """blue | business | government | none, "unknown" when only the legacy ``verified`` flag is set, or None."""
    vt = (post.author_verified_type or "").strip().lower()
    if vt in VERIFIED_TYPES or vt == "none":
        return vt
    if post.author_verified is True:
        return "unknown"
    if post.author_verified is False:
        return "none"
    return None


def _norm_words(text: str | None) -> list[str]:
    """Lowercase words of a display name: accents, emoji and punctuation dropped."""
    if not text:
        return []
    folded = unicodedata.normalize("NFKD", unicodedata.normalize("NFKC", text))
    ascii_only = "".join(ch for ch in folded if not unicodedata.combining(ch))
    return re.findall(r"[a-z0-9]+", ascii_only.casefold())


def _norm_handle(handle: str) -> str:
    return handle.lower().translate(_HANDLE_HOMOGLYPHS).replace("_", "").replace("rn", "m").replace("vv", "w")


def _decoration(extra: str) -> bool:
    """True when ``extra`` is only digits and decoration words ("_ceo", "2", "therealofficial")."""
    rest = re.sub(r"[0-9_]", "", extra.lower())
    if not rest:
        return True
    # rest must split into decoration words (short strings: a tiny word-break search)
    ok = [True] + [False] * len(rest)
    for i in range(1, len(rest) + 1):
        ok[i] = any(ok[j] and rest[j:i] in _HANDLE_DECORATION for j in range(max(0, i - 9), i))
    return ok[len(rest)]


def _contains_handle(username: str, watched_handle: str) -> bool:
    """``username`` is ``watched_handle`` plus decoration: elonmusk_ceo, real_elonmusk, elonmusk2 (not MarketIntel)."""
    raw = username.lower().replace("_", "")
    watched = watched_handle.lower().replace("_", "")
    folded, watched_folded = _norm_handle(username), _norm_handle(watched_handle)
    for name, core in ((raw, watched), (folded, watched_folded)):
        i = name.find(core)
        if i < 0 or name == core or len(name) - len(core) > _HANDLE_EXTRA_CHARS:
            continue
        if _decoration(name[:i]) and _decoration(name[i + len(core) :]):
            return True
    return False


def impersonated_account(
    post: Post, watchlist: Watchlist | None, settings: RedFlagSettings | None = None
) -> tuple[Account, str] | None:
    """(watched account, "display name" | "handle") when the author imitates a watched account it is not.

    Business / government-verified authors are never flagged (X verifies organisations and their affiliates:
    @TeslaCharging is not an impersonator of @Tesla). A handle that merely contains a watched handle imitates it
    only when the rest is decoration (elonmusk_ceo), and not when the account is old and large (an established news
    account such as @MacroStrategy is not imitating @Strategy).
    """
    if watchlist is None:
        return None
    if (post.author_verified_type or "").strip().lower() in ("business", "government"):
        return None
    username = (post.author_username or "").strip().lstrip("@")
    uname = _norm_handle(username) if username else ""
    name_words = _norm_words(post.author_name)
    name_core = [w for w in name_words if w not in _NAME_NOISE]
    age = account_age_days(post)
    big = (settings.established_min_followers if settings is not None else 50_000)
    old_and_large = age is not None and age >= _HANDLE_OLD_DAYS and (post.author_followers or 0) >= big
    for acct in watchlist.accounts:
        if acct.user_id and acct.user_id == post.author_id:
            return None  # it is the watched account itself
        if not acct.user_id and username and acct.handle_lower == username.lower():
            return None  # unverified watchlist entry matched by handle: treated as the account
        expect = [w for w in _norm_words(acct.expect_name) if w]
        if expect and name_words:
            if acct.kind == "person":
                # "Elon Musk", "Elon Musk 🚀", "Elon Musk (CEO)": every word of a two-word+ person's name
                if len(expect) >= 2 and all(w in name_words for w in expect):
                    return acct, "display name"
            else:
                # exactly the company's name ("Eli Lilly and Company", "Tesla Official"), not "Tesla Owners Club"
                core_expect = [w for w in expect if w not in _NAME_NOISE]
                if core_expect and name_core == core_expect:
                    return acct, "display name"
        if uname:
            watched = _norm_handle(acct.handle)
            if uname == watched and username.lower() != acct.handle_lower:
                return acct, "handle"  # tesIa / elonrnusk
            if username.lower() == acct.handle_lower and acct.user_id and acct.user_id != post.author_id:
                return acct, "handle"  # the watched handle now belongs to another account id
            if len(watched) >= _HANDLE_CONTAIN_MIN and not old_and_large and _contains_handle(username, acct.handle):
                return acct, "handle"  # elonmusk_ceo, real_elonmusk
    return None


def _verification_reason(vt: str | None) -> str:
    return {
        "business": "business-verified",
        "government": "government-verified",
        "blue": "blue-verified",
        "unknown": "verified (type unknown)",
        "none": "not verified",
    }.get(vt or "", "verification unknown")


def _fact_reasons(post: Post, vt: str | None, age: int | None) -> list[str]:
    reasons = [_verification_reason(vt)]
    if post.author_followers is not None:
        reasons.append(f"{format_count(post.author_followers)} followers")
    if age is not None:
        reasons.append(format_age_days(age))
    return reasons


def account_brands(account: Account, watchlist: Watchlist | None = None) -> tuple[str, ...]:
    """Brand tokens of a watched account (handle, expected name, its tickers' company names) for look-alike links."""
    names: list[str | None] = [account.handle, account.expect_name]
    if watchlist is not None:
        for t in account.tickers:
            names.extend(watchlist.company_names.get(t, ()))
    return brand_tokens(names)


def compromise_check(post: Post, account: Account, watchlist: Watchlist | None = None) -> CompromiseCheck:
    """Scam signals in a watched account's own words and links (see redflags.detect_compromise)."""
    return detect_compromise(own_text(post), urls=post.urls, brands=account_brands(account, watchlist))


def without_compromise(credibility: Credibility, account: Account, post: Post) -> Credibility:
    """The official credibility of a watched account whose possible-hijack check was overruled (Claude scored the
    post, found no account compromise, and the signals were not definitive)."""
    handle = safe_handle(post.author_username) or safe_handle(account.handle) or "watched account"
    who = "company" if account.kind == "company" else "CEO / person"
    return Credibility(
        level="official",
        reasons=[f"watched {who} account @{handle}"],
        is_official=True,
        link_domains=list(credibility.link_domains),
    )


def assess_credibility(
    post: Post,
    account: Account | None,
    source: Source | None,
    settings: RedFlagSettings,
    watchlist: Watchlist | None = None,
) -> Credibility:
    """How believable the author is (see the module docstring). Never raises for well-formed posts."""
    domains = link_domains(post.urls)
    trusted_links = [d for d in domains if is_trusted_domain(d, settings.trusted_domains)]

    # ---- a watched company / CEO account
    if account is not None:
        handle = safe_handle(post.author_username) or safe_handle(account.handle) or "watched account"
        who = "company" if account.kind == "company" else "CEO / person"
        reasons = [f"watched {who} account @{handle}"]
        check = compromise_check(post, account, watchlist)
        if check.likely:
            reasons.append("posted scam-like crypto content (" + ", ".join(check.signals[:4]) + ")")
            return Credibility(
                level="suspicious",
                reasons=reasons,
                is_official=True,
                possible_compromise=True,
                link_domains=domains,
            )
        return Credibility(level="official", reasons=reasons, is_official=True, link_domains=domains)

    vt = verified_type(post)
    age = account_age_days(post)
    facts = _fact_reasons(post, vt, age)
    followers = post.author_followers or 0
    imitated = None if source is not None else impersonated_account(post, watchlist, settings)
    # a trusted-domain link is a fact about the post (a hint), never a reason to trust the author
    link_reason = [f"links to {d} (trusted domain)" for d in trusted_links[:2]]

    def make(level: str, lead: list[str], **kw: object) -> Credibility:
        return Credibility(
            level=level,  # type: ignore[arg-type]
            reasons=[*lead, *facts, *link_reason],
            source_kind=source.kind if source is not None else None,
            link_domains=domains,
            **kw,  # type: ignore[arg-type]
        )

    # ---- listed sources (vetted in the watchlist): trusted ones, else at least established
    if source is not None:
        kind = source.kind.replace("_", " ")
        if source.trusted:
            return make("trusted", [f"listed trusted source ({kind})"], is_trusted_source=True)
        if vt in ("business", "government") and followers >= settings.trusted_min_followers:
            return make("trusted", [f"listed source ({kind})"])
        return make("established", [f"listed source ({kind})"])

    # ---- red flags about the author itself
    if imitated is not None:
        acct, what = imitated
        lead = [f"{what} imitates watched @{safe_handle(acct.handle) or 'account'} (not that account)"]
        return make("suspicious", lead, possible_impersonation=True)
    if post.author_parody is True:
        return make("suspicious", ["X parody label"])
    if age is not None and age < settings.suspicious_account_age_days:
        return make("suspicious", [f"new account (under {settings.suspicious_account_age_days} days)"])

    # ---- by metrics
    if vt in ("business", "government") and followers >= settings.trusted_min_followers:
        return make("trusted", ["large verified organisation"])
    old_enough = age is None or age >= ESTABLISHED_MIN_AGE_DAYS
    if vt in VERIFIED_TYPES and followers >= settings.established_min_followers and old_enough:
        return make("established", [])
    return make("unverified", [])


def _own_ticker_named(score: Score, account: Account | None) -> bool:
    if account is None:
        return False
    own = {str(t).strip().lstrip("$").upper() for t in account.tickers}
    return any(str(t).strip().lstrip("$").upper() in own for t in score.tickers)


def clamp_claim(score: Score, credibility: Credibility | None, account: Account | None) -> str | None:
    """Cap ``score.claim_status`` by the credibility computed in code (defence in depth against a mistaken or
    injected answer). Returns a short note when the status was lowered, else None.

    * confirmed_official only from the watched account itself (not possibly hijacked) about its own tickers;
      otherwise the claim is unverified (a CEO's allegation about another company is still an allegation);
    * credible_report only from official or trusted authors (Claude cannot open the links);
    * a possible impersonator's claim is likely_false.
    """
    if credibility is None or not score.red_flag:
        return None
    status = score.claim_status
    new: ClaimStatus = status
    if credibility.possible_impersonation and status in ("confirmed_official", "credible_report", "unverified"):
        new = "likely_false"
    elif status == "confirmed_official":
        if not (credibility.is_official and not credibility.possible_compromise and _own_ticker_named(score, account)):
            new = "credible_report" if credibility.level == "trusted" else "unverified"
    elif status == "credible_report" and credibility.level not in ("official", "trusted"):
        new = "unverified"
    if new == status:
        return None
    score.claim_status = new
    if new == "likely_false":
        score.false_news_risk = "high"
    elif new == "unverified" and score.false_news_risk == "low":
        score.false_news_risk = "medium"
    return f"claim status capped by ceoscan: {status} -> {new}"


__all__ = [
    "account_brands",
    "assess_credibility",
    "clamp_claim",
    "compromise_check",
    "without_compromise",
    "account_age_days",
    "format_count",
    "impersonated_account",
    "is_trusted_domain",
    "link_domain",
    "link_domains",
    "safe_handle",
    "verified_type",
]
