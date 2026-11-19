"""Turn a scored post (or an ops event) into an AlertMessage for the notifiers.

Post alert layout (push body)::

    @elonmusk · TSLA, SPCX · BULLISH · 8/10
    Why: <one-line reason>
    "<first snippet_chars characters of the post>…"
    Posted Nov 19, 8:41:07 PM PT
    <optional tag lines: late, joke/meme, edited, reply/quote, quiet hours, rules-only, ...>
    https://x.com/elonmusk/status/...

Title: ``🟢 TSLA +8/10 @elonmusk``, with markers for what makes an alert less reliable or not live:

* rules-only scores (source rules_fallback / offline / skipped: Claude did not score it) get
  ``(rules-only)`` in the title and the SMS, plus a tag line saying why;
* a re-alert for an edited post gets ``(edited)`` and a tag line with what changed;
* late posts (older than ``alerts.max_post_age_seconds`` when received: outage / restart catch-up) get the
  prefix ``Missed during outage - posted 2h ago:`` in the title and SMS.

Post text is untrusted data; it is only ever displayed (whitespace-collapsed, HTML entities decoded),
never interpreted.
"""

from __future__ import annotations

import html
import logging

from ..config import AlertSettings
from ..models import Account, AlertMessage, Decision, Post, Priority, Score
from ..timeutil import format_local
from .decision import format_age, is_late, post_age_seconds

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
) -> AlertMessage:
    """Build the push/SMS/email message for a post that passed ``decide``.

    ``edit_note`` (e.g. "was BULLISH, now BEARISH") marks a re-alert for an edited post. Lateness is
    derived from the post itself (received_at - created_at vs ``settings.max_post_age_seconds``).
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
