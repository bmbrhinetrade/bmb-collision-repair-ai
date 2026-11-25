"""Turn a scored post (or an ops event) into an AlertMessage for the notifiers.

Post alert layout (push body)::

    @elonmusk · TSLA, SPCX · BULLISH · 8/10
    Why: <one-line reason>
    "<first snippet_chars characters of the post>…"
    Posted Nov 19, 8:41:07 PM PT
    <optional tag lines: late, joke/meme, edited, reply/quote, quiet hours, rules-only, ...>
    🧠 Claude analyst brief on the way     (only when the Claude analyst desk will look at it)
    📨 Sent to Claude Duet - ask it about this alert   (only when the Claude Duet feed emails it)
    https://x.com/elonmusk/status/...

Title: ``🟢 TSLA +8/10 @elonmusk``, with markers for what makes an alert less reliable or not live:

* rules-only scores (source rules_fallback / offline / skipped: Claude did not score it) get
  ``(rules-only)`` in the title and the SMS, plus a tag line saying why;
* a re-alert for an edited post gets ``(edited)`` and a tag line with what changed;
* late posts (older than ``alerts.max_post_age_seconds`` when received: outage / restart catch-up) get the
  prefix ``Missed during outage - posted 2h ago:`` in the title and SMS.

Red-flag alerts (:func:`format_red_flag_alert`; bad news about my tickers, see alerts/decision.py):

* ``🚩 RED FLAG $TSLA 7/10 bearish - UNVERIFIED (could be false)`` with a claim badge: CONFIRMED (official) /
  CREDIBLE / UNVERIFIED (could be false) / LIKELY FALSE / DENIED;
* ``🚩 Bad news spreading: $TSLA - 4 accounts in 60 min (all UNVERIFIED)`` (lists the posts and handles with
  credibility labels; the title, SMS and Duet subject carry the strongest claim label in the window: "incl.
  CONFIRMED" / "incl. CREDIBLE" / "all UNVERIFIED" / "all LIKELY FALSE");
* ``🛡️ DENIED by @Tesla: $TSLA`` (links back to the earlier red flags it denies);
* ``⚠️ @handle may be HACKED - do not act on this post`` (a watched account posting scam-like crypto content; the
  post snippet shows "[link removed]" / "[address removed]" instead of the scam's links and wallet addresses).

The wording never states an allegation as fact: the body says "@x alleges" / "@x claims" / "@x reports", the
claim status and credibility come first, the title carries the badge, and an SMS reason for an unconfirmed claim
starts with "claim:". The late / rules-only / edited markers work as for ordinary alerts.

Post text is untrusted data; it is only ever displayed (whitespace-collapsed, HTML entities decoded),
never interpreted.
"""

from __future__ import annotations

import html
import logging
import re
from collections.abc import Sequence
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

from ..config import AlertSettings
from ..models import Account, AlertMessage, Credibility, Decision, Post, Priority, RedFlagSignal, Score
from ..scoring.credibility import safe_handle
from ..scoring.redflags import type_label
from ..timeutil import format_local, from_ms
from .decision import format_age, is_late, post_age_seconds, strongest_claim

logger = logging.getLogger(__name__)

DIRECTION_EMOJI: dict[str, str] = {"bullish": "🟢", "bearish": "🔴", "unclear": "⚪"}
DIRECTION_SIGN: dict[str, str] = {"bullish": "+", "bearish": "-", "unclear": ""}
ELLIPSIS = "…"
SMS_MAX_CHARS = 300
TITLE_MAX_TICKERS = 3

#: score sources that are not a Claude score (keyword heuristics only)
RULES_ONLY_SOURCES: frozenset[str] = frozenset({"rules_fallback", "offline", "skipped"})
RULES_ONLY_MARKER = "rules-only"
EDITED_MARKER = "edited"
#: Body line for alerts the Claude analyst desk (alerts/claude_desk.py) will analyze.
BRIEF_LINE = "🧠 Claude analyst brief on the way"
#: Body line for alerts the Claude Duet feed (alerts/duet_feed.py) emails to the inbox Claude Duet reads.
DUET_LINE = "📨 Sent to Claude Duet - ask it about this alert"
_RULES_ONLY_WHY: dict[str, str] = {
    "rules_fallback": "Claude unavailable",
    "offline": "Claude scoring not configured",
    "skipped": "not sent to Claude",
}


def is_rules_only(score: Score) -> bool:
    """True when the score came from the fast rules, not from Claude."""
    return score.source in RULES_ONLY_SOURCES


def late_label(age_seconds: float) -> str:
    """'Missed during outage - posted 2h ago' (title / SMS prefix for late posts)."""
    return f"Missed during outage - posted {format_age(age_seconds)} ago"


def one_line(text: str | None) -> str:
    """Collapse all whitespace (incl. newlines) to single spaces."""
    return " ".join((text or "").split())


def truncate(text: str, limit: int, ellipsis: str = ELLIPSIS) -> str:
    """Return ``text`` cut so that the result (ellipsis included) is at most ``limit`` chars."""
    if limit <= 0:
        return ""
    if len(text) <= limit:
        return text
    if len(ellipsis) >= limit:
        return text[:limit]
    return text[: limit - len(ellipsis)].rstrip() + ellipsis


def snippet(text: str, chars: int) -> str:
    """First ``chars`` characters of the (whitespace-collapsed) post, plus "…" when cut.

    The text part is at most ``chars`` characters; the ellipsis is appended after it.
    """
    if chars <= 0:
        return ""
    clean = one_line(html.unescape(text or ""))
    if len(clean) <= chars:
        return clean
    return clean[:chars].rstrip() + ELLIPSIS


def _handle(post: Post, account: Account | None) -> str:
    if post.author_username:
        return f"@{post.author_username}"
    if account is not None:
        return f"@{account.handle}"
    return f"user {post.author_id}"


def _tickers(score: Score) -> list[str]:
    out: list[str] = []
    for t in score.tickers:
        t = str(t).strip().lstrip("$").upper()
        if t and t not in out:
            out.append(t)
    return out


def _title_tickers(tickers: list[str]) -> str:
    shown = ",".join(tickers[:TITLE_MAX_TICKERS])
    extra = len(tickers) - TITLE_MAX_TICKERS
    return f"{shown}+{extra}" if extra > 0 else shown


def _tag_lines(
    post: Post,
    account: Account | None,
    score: Score,
    decision: Decision,
    late_age: float | None,
    edit_note: str | None,
    settings: AlertSettings,
) -> list[str]:
    lines: list[str] = []
    if late_age is not None:
        received = format_local(post.received_at, settings.timezone)
        lines.append(f"⏪ Late: received {received}, {format_age(late_age)} after it was posted (catch-up)")
    if score.is_joke:
        lines.append("😜 Joke/meme")
    if edit_note:
        lines.append(f"✏️ Edited: {one_line(edit_note)}")
    elif post.is_edit:
        lines.append("✏️ Edited post")
    for ref in post.referenced:
        who = f" @{ref.author_username}" if ref.author_username else ""
        if ref.type == "replied_to":
            lines.append(f"↩️ Reply to{who or ' a post'}")
        elif ref.type == "quoted":
            lines.append(f"💬 Quotes{who or ' a post'}")
    if account is not None and account.always_alert:
        lines.append("📌 Always-alert account")
    if decision.quiet_hours:
        lines.append("🌙 Quiet hours")
    if is_rules_only(score):
        why = _RULES_ONLY_WHY.get(score.source, "Claude did not score it")
        lines.append(f"ℹ️ Rules-only score ({why}) - keyword heuristics, not an AI judgment")
    return lines


def _sms_text(
    handle: str,
    tickers: list[str],
    direction: str,
    impact: int,
    reason: str,
    url: str,
    markers: list[str] | None = None,
    prefix: str | None = None,
) -> str:
    parts = [handle, ",".join(tickers), direction.upper(), f"{impact}/10"]
    if markers:
        parts.append(f"({', '.join(markers)})")
    head = " ".join(p for p in parts if p)
    if prefix:
        head = f"[{prefix}] {head}"
    tail = f" {url}"
    room = SMS_MAX_CHARS - len(head) - len(tail) - 2  # ": "
    if reason and room > 3:
        text = f"{head}: {truncate(reason, room, '...')}{tail}"
    else:
        text = f"{head}{tail}"
    return truncate(text, SMS_MAX_CHARS, "...")


def format_post_alert(
    post: Post,
    account: Account | None,
    score: Score,
    decision: Decision,
    settings: AlertSettings,
    *,
    edit_note: str | None = None,
    desk_brief: bool = False,
    duet_line: bool = False,
) -> AlertMessage:
    """Build the push/SMS/email message for a post that passed ``decide``.

    ``edit_note`` (e.g. "was BULLISH, now BEARISH") marks a re-alert for an edited post. Lateness is
    derived from the post itself (received_at - created_at vs ``settings.max_post_age_seconds``).
    ``desk_brief`` adds the :data:`BRIEF_LINE` (the Claude analyst desk is enabled, configured and will
    analyze this alert); ``duet_line`` adds the :data:`DUET_LINE` right after it (the Claude Duet feed is
    enabled, configured and will email this alert).
    """
    handle = _handle(post, account)
    tickers = _tickers(score)
    direction = score.direction if score.direction in DIRECTION_EMOJI else "unclear"
    impact = int(score.impact)
    reason = one_line(score.reason)
    age = post_age_seconds(post)
    late_age = age if is_late(age, settings) else None
    markers: list[str] = []
    if edit_note:
        markers.append(EDITED_MARKER)
    if is_rules_only(score):
        markers.append(RULES_ONLY_MARKER)
    prefix = late_label(late_age) if late_age is not None else None

    title = " ".join(
        p
        for p in (
            f"{prefix}:" if prefix else "",
            DIRECTION_EMOJI[direction],
            _title_tickers(tickers),
            f"{DIRECTION_SIGN[direction]}{impact}/10",
            handle,
            f"({', '.join(markers)})" if markers else "",
        )
        if p
    )

    header = " · ".join(p for p in (handle, ", ".join(tickers), direction.upper(), f"{impact}/10") if p)
    lines = [header]
    if reason:
        lines.append(f"Why: {reason}")
    snip = snippet(post.text, settings.snippet_chars)
    if snip:
        lines.append(f'"{snip}"')
    lines.append(f"Posted {format_local(post.created_at, settings.timezone)}")
    lines.extend(_tag_lines(post, account, score, decision, late_age, edit_note, settings))
    if desk_brief:
        lines.append(BRIEF_LINE)
    if duet_line:
        lines.append(DUET_LINE)
    lines.append(post.url)

    tags = [*tickers, direction]
    if score.is_joke:
        tags.append("joke")
    if decision.quiet_hours:
        tags.append("quiet-hours")
    if late_age is not None:
        tags.append("late")
    tags.extend(markers)

    return AlertMessage(
        title=title,
        body="\n".join(lines),
        url=post.url,
        priority=decision.priority,
        tags=tags,
        sms_text=_sms_text(handle, tickers, direction, impact, reason, post.url, markers, prefix),
        kind="post",
    )


# --------------------------------------------------------------------------- red flags

RED_FLAG_EMOJI = "🚩"
DENIAL_EMOJI = "🛡️"
HACKED_EMOJI = "⚠️"
#: claim status -> badge (title / body) and short badge (lists, SMS, tags)
CLAIM_BADGES: dict[str, str] = {
    "confirmed_official": "CONFIRMED (official)",
    "credible_report": "CREDIBLE",
    "unverified": "UNVERIFIED (could be false)",
    "likely_false": "LIKELY FALSE",
    "denial": "DENIED",
}
CLAIM_SHORT: dict[str, str] = {
    "confirmed_official": "CONFIRMED",
    "credible_report": "CREDIBLE",
    "unverified": "UNVERIFIED",
    "likely_false": "LIKELY FALSE",
    "denial": "DENIED",
}
_CLAIM_BY_LEVEL = {"official": "confirmed_official", "trusted": "credible_report"}
#: how many related posts a spreading / denial alert lists
MAX_LISTED_POSTS = 6
HACKED_ADVICE = (
    "Do not buy, sell, send crypto, connect a wallet or click links because of this post until the company "
    "confirms it on another channel."
)


# links and wallet addresses in a hijack snippet: the alert must not hand the user the scam's phishing link
_SNIPPET_URL_RE = re.compile(r"(?:https?://|www\.)\S{1,300}|(?<![\w@.-])[a-z0-9-]{1,63}(?:\.[a-z0-9-]{1,63}){0,4}"
                             r"\.(?:com|io|xyz|app|net|org|fun|meme|so|ag|ai|money|co|gg|me|finance|site|online|top"
                             r"|live|claims?|link|click|info|dev|vip|pro|biz)(?:/\S{0,300})?(?![\w-])", re.IGNORECASE)
_SNIPPET_ADDRESS_RE = re.compile(
    r"(?<![0-9A-Za-z])0x[0-9a-fA-F]{40}(?![0-9A-Za-z])|(?<![0-9A-Za-z])bc1[02-9ac-hj-np-z]{25,62}(?![0-9A-Za-z])"
    r"|(?<![1-9A-HJ-NP-Za-km-z0OIl])[1-9A-HJ-NP-Za-km-z]{32,44}(?![1-9A-HJ-NP-Za-km-z0OIl])"
)


def defang(text: str) -> str:
    """``text`` with links replaced by "[link removed]" and wallet addresses by "[address removed]"."""
    text = _SNIPPET_ADDRESS_RE.sub("[address removed]", text)
    return _SNIPPET_URL_RE.sub("[link removed]", text)


def spread_claim_label(statuses: Sequence[str | None]) -> str:
    """'incl. CONFIRMED' / 'incl. CREDIBLE' / 'all UNVERIFIED' / 'all LIKELY FALSE' for a spreading alert."""
    strongest = strongest_claim(statuses)
    if strongest in ("confirmed_official", "credible_report"):
        return f"incl. {CLAIM_SHORT[strongest]}"
    if strongest in ("unverified", "likely_false"):
        return f"all {CLAIM_SHORT[strongest]}"
    return "all UNVERIFIED"


def claim_status_of(score: Score, credibility: Credibility | None) -> str:
    """The score's claim status; "not_applicable" falls back to the source (official / trusted / unverified)."""
    status = str(score.claim_status or "not_applicable")
    if status in CLAIM_BADGES:
        return status
    level = credibility.level if credibility is not None else None
    return _CLAIM_BY_LEVEL.get(level or "", "unverified")


def claim_badge(status: str) -> str:
    return CLAIM_BADGES.get(status, "UNVERIFIED (could be false)")


def credibility_label(level: str | None, source_kind: str | None = None) -> str:
    """'suspicious', 'established (short seller)', 'trusted (regulator)' ..."""
    label = level or "unknown source"
    if source_kind and source_kind != "other":
        label += f" ({source_kind.replace('_', ' ')})"
    return label


#: red-flag types that are allegations by nature (a short seller's thesis, fraud, a lawsuit)
_ALLEGATION_TYPES = frozenset({"short_report", "fraud_accounting", "legal_regulatory"})


def _claim_verb(status: str, credibility: Credibility | None, types: Sequence[str] = ()) -> str:
    """Never state an allegation as fact: official / credible sources "report", others "allege" / "claim"."""
    if status in ("confirmed_official", "credible_report"):
        return "reports"
    if (credibility is not None and credibility.source_kind == "short_seller") or _ALLEGATION_TYPES & set(types):
        return "alleges"
    return "claims"


def _cashtags(tickers: Sequence[str]) -> str:
    shown = " ".join(f"${t}" for t in tickers[:TITLE_MAX_TICKERS])
    extra = len(tickers) - TITLE_MAX_TICKERS
    return f"{shown} +{extra}" if extra > 0 else shown


def _short_time(dt: datetime, tz: str) -> str:
    local = dt.astimezone(ZoneInfo(tz))
    hour12 = local.strftime("%I").lstrip("0") or "12"
    return f"{hour12}:{local.strftime('%M %p')}"


def _event_line(event: dict[str, Any], tz: str, *, with_url: bool = False) -> str:
    """'• @x - UNVERIFIED, suspicious · short report · 10:02 AM (alerted)' for one tracked red-flag post."""
    handle = safe_handle(event.get("handle"))
    who = f"@{handle}" if handle else f"user {event.get('author_id')}"
    status = str(event.get("claim_status") or "unverified")
    label = credibility_label(event.get("credibility"), event.get("source_kind"))
    if event.get("credibility") == "official" and not event.get("official"):
        label = "watched account, about another company"  # a CEO's claim about someone else's stock
    types = [type_label(t) for t in (event.get("types") or [])][:2]
    line = f"• {who} - {CLAIM_SHORT.get(status, 'UNVERIFIED')}, {label}"
    if types:
        line += " · " + ", ".join(types)
    created = event.get("created_at_ms")
    if isinstance(created, int):
        line += " · " + _short_time(from_ms(created), tz)
    if event.get("alerted"):
        line += " (alerted)"
    if with_url and handle and event.get("post_id"):
        line += f" https://x.com/{handle}/status/{event['post_id']}"
    return line


def _red_flag_sms(head: str, reason: str, url: str, prefix: str | None) -> str:
    if prefix:
        head = f"[{prefix}] {head}"
    tail = f" {url}"
    room = SMS_MAX_CHARS - len(head) - len(tail) - 2
    text = f"{head}: {truncate(reason, room, '...')}{tail}" if reason and room > 3 else f"{head}{tail}"
    return truncate(text, SMS_MAX_CHARS, "...")


def format_red_flag_alert(
    post: Post,
    account: Account | None,
    score: Score,
    decision: Decision,
    settings: AlertSettings,
    *,
    kind: str,
    credibility: Credibility | None = None,
    signal: RedFlagSignal | None = None,
    related: Sequence[dict[str, Any]] = (),
    compromise_signals: Sequence[str] = (),
    edit_note: str | None = None,
    desk_brief: bool = False,
    duet_line: bool = False,
) -> AlertMessage:
    """Push / SMS / email for a red-flag alert. ``kind``: red_flag | spreading | denial | compromise.

    ``related`` are the tracker's events (oldest first) behind a spreading or denial alert, each optionally
    with ``alerted`` (an alert went out for it). ``compromise_signals`` are fixed labels from
    scoring/redflags.py (never post text).
    """
    handle = _handle(post, account)
    tz = settings.timezone
    status = claim_status_of(score, credibility)
    direction = score.direction if score.direction in DIRECTION_EMOJI else "unclear"
    impact = int(score.impact)
    reason = one_line(score.reason)
    note = one_line(score.credibility_note)
    tickers = _tickers(score)
    if signal is not None and signal.ticker and kind in ("spreading", "denial"):
        tickers = [signal.ticker, *[t for t in tickers if t != signal.ticker]]
    age = post_age_seconds(post)
    late_age = age if is_late(age, settings) else None
    prefix = late_label(late_age) if late_age is not None else None
    markers: list[str] = []
    if edit_note:
        markers.append(EDITED_MARKER)
    if is_rules_only(score) and kind != "compromise":  # a hijack is detected in code, Claude or not
        markers.append(RULES_ONLY_MARKER)
    cred_label = credibility_label(credibility.level, credibility.source_kind) if credibility is not None else None
    snip = snippet(post.text, settings.snippet_chars)
    lines: list[str] = []
    tag_kind: list[str] = []

    if kind == "compromise":
        headline = f"{HACKED_EMOJI} {handle} may be HACKED - do not act on this post"
        mapped = ", ".join(f"${t}" for t in (account.tickers if account is not None else tickers))
        lines.append(
            f"{HACKED_EMOJI} {handle} (watched account{', ' + mapped if mapped else ''}) posted scam-like crypto "
            "content. The account may be hijacked."
        )
        lines.append(HACKED_ADVICE)
        if compromise_signals:
            lines.append("Signals: " + ", ".join(compromise_signals[:6]))
        safe_snip = snippet(defang((post.text or "")[:2000]), settings.snippet_chars)
        if safe_snip:
            lines.append(f'Post (links removed): "{safe_snip}"')
        sms_head = f"{handle} may be HACKED - do not act on its post"
        sms_reason = ""
        tag_kind = ["compromise"]
    elif kind == "spreading":
        sig = signal or RedFlagSignal(ticker=tickers[0] if tickers else "")
        n, minutes = sig.distinct_authors, sig.window_minutes
        spread_label = spread_claim_label([*(e.get("claim_status") for e in related), score.claim_status])
        headline = (
            f"{RED_FLAG_EMOJI} Bad news spreading: ${sig.ticker} - {n} accounts in {minutes} min ({spread_label})"
        )
        lines.append(
            f"{n} different accounts posted bad news about ${sig.ticker} in {minutes} min ({spread_label}). "
            "Claims are not checked - verify before acting."
        )
        for e in list(related)[-MAX_LISTED_POSTS:]:
            lines.append(_event_line(e, tz))
        if len(related) > MAX_LISTED_POSTS:
            lines.append(f"... and {len(related) - MAX_LISTED_POSTS} earlier post(s)")
        if sig.confirmed_by:
            lines.append("Confirmed / credibly reported by: " + ", ".join(sig.confirmed_by[:4]))
        else:
            lines.append("No official confirmation or credible report yet.")
        verb = _claim_verb(status, credibility, score.red_flag_types)
        lines.append(f"Latest: {handle} {verb} ({claim_badge(status)}):")
        if snip:
            lines.append(f'"{snip}"')
        if reason:
            lines.append(f"Why: {reason}")
        sms_head = f"Bad news spreading ${sig.ticker} ({spread_label}): {n} accounts/{minutes} min"
        if sig.confirmed_by:
            sms_head += f" ({len(sig.confirmed_by)} official/trusted)"
        sms_reason = ""
        tag_kind = ["red-flag", "spreading"]
    elif kind == "denial":
        sig = signal or RedFlagSignal(ticker=tickers[0] if tickers else "")
        who = ", ".join(sig.denied_by) or handle
        headline = f"{DENIAL_EMOJI} DENIED by {who}: ${sig.ticker}"
        lines.append(f"{handle} (watched account) denies recent bad news about ${sig.ticker}:")
        if snip:
            lines.append(f'"{snip}"')
        if related:
            lines.append(f"Denies {len(related)} earlier red flag{'s' if len(related) != 1 else ''}:")
            for e in list(related)[-MAX_LISTED_POSTS:]:
                lines.append(_event_line(e, tz, with_url=True))
        if reason:
            lines.append(f"Why: {reason}")
        sms_head = f"DENIED by {who} ${sig.ticker}"
        sms_reason = reason
        tag_kind = ["denied"]
    else:  # red_flag
        headline = (
            f"{RED_FLAG_EMOJI} RED FLAG {_cashtags(tickers)} {impact}/10 {direction} - {claim_badge(status)}"
        )
        header = " · ".join(p for p in (", ".join(f"${t}" for t in tickers), direction.upper(), f"{impact}/10") if p)
        who = handle + (f" ({cred_label})" if cred_label else "")
        verb = _claim_verb(status, credibility, score.red_flag_types)
        lines.append(f"{RED_FLAG_EMOJI} {who} {verb} bad news: {header}")
        lines.append(f"Claim: {claim_badge(status)} · false-news risk: {score.false_news_risk}")
        if note:
            lines.append(f"Note: {note}")
        if credibility is not None and credibility.reasons:
            lines.append("Source: " + "; ".join(credibility.reasons[:5]))
        if score.red_flag_types:
            lines.append("Type: " + ", ".join(type_label(t) for t in score.red_flag_types))
        if reason:
            lines.append(f"Why: {reason}")
        if snip:
            lines.append(f'"{snip}"')
        if signal is not None and signal.distinct_authors > 1:
            lines.append(
                f"📣 {signal.distinct_authors} accounts posted bad news about ${signal.ticker} "
                f"in the last {signal.window_minutes} min"
            )
        if signal is not None and signal.confirmed_by:
            others = [h for h in signal.confirmed_by if h.lower() != handle.lower()]
            if others:
                lines.append("Also confirmed / credibly reported by: " + ", ".join(others[:4]))
        sms_head = f"RED FLAG {_cashtags(tickers)} {impact}/10 {CLAIM_SHORT.get(status, 'UNVERIFIED')} {handle}"
        # never state an unconfirmed claim as fact in the user's main text channel
        sms_reason = reason if status in ("confirmed_official", "credible_report") or not reason else f"claim: {reason}"
        tag_kind = ["red-flag"]

    title = " ".join(
        part
        for part in (
            f"{prefix}:" if prefix else "",
            headline,
            f"({', '.join(markers)})" if markers else "",
        )
        if part
    )
    lines.append(f"Posted {format_local(post.created_at, tz)}")
    lines.extend(_tag_lines(post, account, score, decision, late_age, edit_note, settings))
    if desk_brief:
        lines.append(BRIEF_LINE)
    if duet_line:
        lines.append(DUET_LINE)
    lines.append(post.url)

    tags = [*tickers, *tag_kind]
    if kind in ("red_flag", "spreading"):
        tags.append(CLAIM_SHORT.get(status, "UNVERIFIED").lower().replace(" ", "-"))
    tags.append(direction if kind != "compromise" else "do-not-act")
    if decision.quiet_hours:
        tags.append("quiet-hours")
    if late_age is not None:
        tags.append("late")
    tags.extend(markers)

    return AlertMessage(
        title=title,
        body="\n".join(lines),
        url=post.url,
        priority=decision.priority,
        tags=list(dict.fromkeys(tags)),
        sms_text=_red_flag_sms(sms_head, sms_reason, post.url, prefix),
        kind="post",
    )


def format_system_alert(title: str, body: str, priority: Priority = "high") -> AlertMessage:
    """An ops/system alert (stream down, recovered, credits depleted, startup ...)."""
    sms = truncate(one_line(f"ceoscan: {title} - {body}" if body else f"ceoscan: {title}"), SMS_MAX_CHARS, "...")
    return AlertMessage(
        title=title,
        body=body,
        url=None,
        priority=priority,
        tags=["warning"] if priority in ("high", "urgent") else [],
        sms_text=sms,
        kind="system",
    )
