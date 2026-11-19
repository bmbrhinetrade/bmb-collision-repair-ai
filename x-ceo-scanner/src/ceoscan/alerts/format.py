"""Turn a scored post (or an ops event) into an AlertMessage for the notifiers.

Post alert layout (push body)::

    @elonmusk · TSLA, SPCX · BULLISH · 8/10
    Why: <one-line reason>
    "<first snippet_chars characters of the post>…"
    Posted Nov 19, 8:41:07 PM PT
    <optional tag lines: joke/meme, edited, reply/quote, quiet hours, ...>
    https://x.com/elonmusk/status/...

Post text is untrusted data; it is only ever displayed (whitespace-collapsed, HTML entities decoded),
never interpreted.
"""

from __future__ import annotations

import html
import logging

from ..config import AlertSettings
from ..models import Account, AlertMessage, Decision, Post, Priority, Score
from ..timeutil import format_local

logger = logging.getLogger(__name__)

DIRECTION_EMOJI: dict[str, str] = {"bullish": "🟢", "bearish": "🔴", "unclear": "⚪"}
DIRECTION_SIGN: dict[str, str] = {"bullish": "+", "bearish": "-", "unclear": ""}
ELLIPSIS = "…"
SMS_MAX_CHARS = 300
TITLE_MAX_TICKERS = 3


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


def _tag_lines(post: Post, account: Account | None, score: Score, decision: Decision) -> list[str]:
    lines: list[str] = []
    if score.is_joke:
        lines.append("😜 Joke/meme")
    if post.is_edit:
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
    if score.source == "rules_fallback":
        lines.append("ℹ️ Rules-only score (Claude unavailable)")
    return lines


def _sms_text(handle: str, tickers: list[str], direction: str, impact: int, reason: str, url: str) -> str:
    head = " ".join(p for p in (handle, ",".join(tickers), direction.upper(), f"{impact}/10") if p)
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
) -> AlertMessage:
    """Build the push/SMS/email message for a post that passed ``decide``."""
    handle = _handle(post, account)
    tickers = _tickers(score)
    direction = score.direction if score.direction in DIRECTION_EMOJI else "unclear"
    impact = int(score.impact)
    reason = one_line(score.reason)

    title = " ".join(
        p
        for p in (
            DIRECTION_EMOJI[direction],
            _title_tickers(tickers),
            f"{DIRECTION_SIGN[direction]}{impact}/10",
            handle,
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
    lines.extend(_tag_lines(post, account, score, decision))
    lines.append(post.url)

    tags = [*tickers, direction]
    if score.is_joke:
        tags.append("joke")
    if decision.quiet_hours:
        tags.append("quiet-hours")

    return AlertMessage(
        title=title,
        body="\n".join(lines),
        url=post.url,
        priority=decision.priority,
        tags=tags,
        sms_text=_sms_text(handle, tickers, direction, impact, reason, post.url),
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
