"""Alert decision: should this scored post push an alert, and at what priority?

Pure function of (score, rules, account, settings, time, post age) so it is trivially unit-testable.

Rules (see SPEC "alerts/decision.py"):

* no score -> no alert;
* quiet hours = ``alerts.quiet_hours.enabled`` and local time inside ``[start, end)`` (overnight windows OK);
* always-alert accounts alert on every post, except during quiet hours (unless ``always_alert_bypass``)
  where they need ``quiet_hours_threshold`` like everyone else;
* everyone else needs ``threshold`` (``quiet_hours_threshold`` during quiet hours), jokes/memes need
  ``joke_threshold``, and with ``require_my_ticker`` the score must name one of my tickers;
* priority: impact >= 9 "urgent", >= 7 "high", otherwise "normal";
* late posts (older than ``alerts.max_post_age_seconds`` when received, i.e. caught up after an outage or
  restart) still alert by the rules above, but never above "normal": "normal" at impact >= 9, else "low".
  The pipeline also keeps them off SMS and out of the latency stats.
"""

from __future__ import annotations

import logging
from datetime import datetime

from ..config import AlertSettings, Settings
from ..models import Account, Decision, Post, Priority, RuleResult, Score
from ..timeutil import in_window

logger = logging.getLogger(__name__)

URGENT_MIN_IMPACT = 9
HIGH_MIN_IMPACT = 7


def _norm_ticker(t: str) -> str:
    return str(t).strip().lstrip("$").upper()


def priority_for(impact: int) -> Priority:
    """Map an impact score (1-10) to an alert priority."""
    if impact >= URGENT_MIN_IMPACT:
        return "urgent"
    if impact >= HIGH_MIN_IMPACT:
        return "high"
    return "normal"


def late_priority_for(impact: int) -> Priority:
    """Priority for a late (caught-up) post: never urgent or high - the news is already old."""
    return "normal" if impact >= URGENT_MIN_IMPACT else "low"


# --------------------------------------------------------------------------- post age


def post_age_seconds(post: Post) -> float:
    """How old the post was when we received it (received_at - created_at), never negative."""
    return max(0.0, (post.received_at - post.created_at).total_seconds())


def is_late(age_seconds: float | None, settings: AlertSettings) -> bool:
    """True when a post of this age is past ``alerts.max_post_age_seconds`` (outage / restart catch-up)."""
    if age_seconds is None:
        return False
    return age_seconds > float(settings.max_post_age_seconds)


def is_late_post(post: Post, settings: AlertSettings) -> bool:
    return is_late(post_age_seconds(post), settings)


def format_age(seconds: float) -> str:
    """Compact human age: 45s, 16m, 2h, 2h 5m, 3d, 3d 4h."""
    s = max(0, int(seconds))
    if s < 60:
        return f"{s}s"
    m = s // 60
    if m < 60:
        return f"{m}m"
    h, m = divmod(m, 60)
    if h < 24:
        return f"{h}h {m}m" if m and h < 10 else f"{h}h"
    d, h = divmod(h, 24)
    return f"{d}d {h}h" if h and d < 10 else f"{d}d"


# --------------------------------------------------------------------------- decision


def is_quiet_hours(settings: Settings, now: datetime) -> bool:
    """True when quiet hours are enabled and ``now`` falls inside the local quiet window."""
    qh = settings.alerts.quiet_hours
    return bool(qh.enabled) and in_window(now, qh.start, qh.end, settings.alerts.timezone)


def _alert(reason: str, quiet: bool, impact: int, late: bool, age: float | None) -> Decision:
    if late:
        return Decision(
            True,
            f"{reason} (late: posted {format_age(age or 0)} ago)",
            quiet_hours=quiet,
            priority=late_priority_for(impact),
        )
    return Decision(True, reason, quiet_hours=quiet, priority=priority_for(impact))


def decide(
    score: Score | None,
    rules: RuleResult,
    account: Account | None,
    settings: Settings,
    now: datetime,
    my_tickers: tuple[str, ...],
    *,
    age_seconds: float | None = None,
) -> Decision:
    """Decide whether a post should alert. Never raises for well-formed inputs.

    ``age_seconds`` (the post's received_at - created_at, see :func:`post_age_seconds`) marks late
    posts; see the module docstring.
    """
    quiet = is_quiet_hours(settings, now)
    late = is_late(age_seconds, settings.alerts)

    if score is None:
        return Decision(
            should_alert=False,
            reason="not flagged" if not rules.flagged else "not scored",
            quiet_hours=quiet,
        )

    sc = settings.scoring
    impact = int(score.impact)
    quiet_min = int(sc.quiet_hours_threshold)

    # ---------------------------------------------------------------- always-alert accounts
    if account is not None and account.always_alert:
        who = f"always-alert account @{account.handle}"
        bypass = bool(settings.alerts.quiet_hours.always_alert_bypass)
        if quiet and not bypass and impact < quiet_min:
            return Decision(False, f"quiet hours: {impact} < {quiet_min} ({who})", quiet_hours=True)
        reason = who
        if quiet:
            if impact < quiet_min:  # only reachable with the bypass on
                reason += " (quiet-hours bypass)"
            else:
                reason += f" (quiet hours: {impact} >= {quiet_min})"
        return _alert(reason, quiet, impact, late, age_seconds)

    # ---------------------------------------------------------------- everyone else
    required = quiet_min if quiet else int(sc.threshold)
    if impact < required:
        if quiet:
            return Decision(False, f"quiet hours: {impact} < {required}", quiet_hours=True)
        return Decision(False, f"below threshold: {impact} < {required}", quiet_hours=False)

    joke_min = int(sc.joke_threshold)
    if score.is_joke and impact < joke_min:
        return Decision(False, f"joke/meme: {impact} < {joke_min}", quiet_hours=quiet)

    if sc.require_my_ticker:
        mine = {_norm_ticker(t) for t in my_tickers}
        hits = [t for t in (_norm_ticker(x) for x in score.tickers) if t in mine]
        if not hits:
            named = ", ".join(_norm_ticker(t) for t in score.tickers) or "none"
            return Decision(False, f"no watched ticker (tickers: {named})", quiet_hours=quiet)

    reason = f"score {impact} >= {required}"
    if quiet:
        reason = f"quiet hours: {reason}"
    if score.is_joke:
        reason += f" (joke/meme >= {joke_min})"
    return _alert(reason, quiet, impact, late, age_seconds)


__all__ = [
    "HIGH_MIN_IMPACT",
    "URGENT_MIN_IMPACT",
    "decide",
    "format_age",
    "is_late",
    "is_late_post",
    "is_quiet_hours",
    "late_priority_for",
    "post_age_seconds",
    "priority_for",
]
