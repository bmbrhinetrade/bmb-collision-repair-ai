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

Red flags (``red_flags`` settings; the pipeline picks the kind, these functions decide):

* :func:`decide_red_flag` - a post with ``score.red_flag`` that names one of my tickers alerts at impact
  >= ``red_flags.min_score`` (6) instead of ``scoring.threshold``; jokes (``joke_threshold``) apply as usual;
  unverified / likely-false claims are dropped when ``alert_unverified`` / ``alert_likely_false`` are off. An
  unverified claim alerts at most "high", a likely-false one at most "normal" (no "urgent" wake-up for a rumor;
  the pipeline also keeps their dispatch score below Pushover priority 1, and a likely-false one below SMS).
  During quiet hours a red flag at ``quiet_hours_threshold`` alerts as usual; one at the normal red-flag bar is
  delivered silently (priority "low", no SMS), so bad news posted overnight - often reported the next day - is
  waiting on the phone at market open instead of being dropped.
* :func:`decide_spreading` / :func:`decide_denial` - always alert, at least "high" / "normal" and higher when the
  impact calls for it; a spreading alert whose posts are all UNVERIFIED / LIKELY FALSE is capped like those claims.
  During quiet hours they are delivered silently (priority "low", no SMS) unless the impact reaches
  ``quiet_hours_threshold``, so bad news that spreads at 3 AM is waiting on the phone at market open.
* :func:`decide_compromise` - a watched account that looks hijacked: "high" priority, ignores quiet hours (it
  protects the user from acting on a fake post); repeats within the cooldown are "low".
"""

from __future__ import annotations

import logging
from collections.abc import Iterable
from datetime import datetime

from ..config import AlertSettings, Settings
from ..models import Account, Decision, Post, Priority, RedFlagSignal, RuleResult, Score
from ..timeutil import in_window

logger = logging.getLogger(__name__)

URGENT_MIN_IMPACT = 9
HIGH_MIN_IMPACT = 7


def _norm_ticker(t: str) -> str:
    return str(t).strip().lstrip("$").upper()


_PRIORITY_ORDER: tuple[Priority, ...] = ("low", "normal", "high", "urgent")
#: the highest priority a red flag with this claim status gets (no urgent wake-up for a rumor)
CLAIM_PRIORITY_CAP: dict[str, Priority] = {"unverified": "high", "likely_false": "normal"}


def _cap_priority(priority: Priority, cap: Priority) -> Priority:
    return priority if _PRIORITY_ORDER.index(priority) <= _PRIORITY_ORDER.index(cap) else cap


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


# --------------------------------------------------------------------------- red flags


def red_flag_tickers(score: Score | None, my_tickers: tuple[str, ...]) -> list[str]:
    """The score's tickers that are mine (normalized, in order)."""
    if score is None:
        return []
    mine = {_norm_ticker(t) for t in my_tickers}
    return [t for t in dict.fromkeys(_norm_ticker(x) for x in score.tickers) if t in mine]


def decide_red_flag(
    score: Score | None,
    rules: RuleResult,
    account: Account | None,
    settings: Settings,
    now: datetime,
    my_tickers: tuple[str, ...],
    *,
    age_seconds: float | None = None,
) -> Decision | None:
    """The decision for a red-flag post about my tickers, or None when it is not one (use :func:`decide`).

    See the module docstring. Always-alert accounts keep their usual decision (labeled as a red flag).
    """
    rf = settings.red_flags
    if not rf.enabled or score is None or not score.red_flag:
        return None
    if not red_flag_tickers(score, my_tickers):
        return None
    status = score.claim_status
    quiet = is_quiet_hours(settings, now)
    if status == "unverified" and not rf.alert_unverified:
        return Decision(False, "red flag: unverified (red_flags.alert_unverified is off)", quiet_hours=quiet)
    if status == "likely_false" and not rf.alert_likely_false:
        return Decision(False, "red flag: likely false (red_flags.alert_likely_false is off)", quiet_hours=quiet)

    if account is not None and account.always_alert:
        base = decide(score, rules, account, settings, now, my_tickers, age_seconds=age_seconds)
        if base.should_alert:
            base.reason = f"red flag: {base.reason}"
        return base

    sc = settings.scoring
    impact = int(score.impact)
    normal_min = min(int(rf.min_score), int(sc.threshold))
    quiet_min = int(sc.quiet_hours_threshold)
    if impact < normal_min:
        return Decision(False, f"red flag below threshold: {impact} < {normal_min}", quiet_hours=quiet)
    joke_min = int(sc.joke_threshold)
    if score.is_joke and impact < joke_min:
        return Decision(False, f"red flag joke/meme: {impact} < {joke_min}", quiet_hours=quiet)
    late = is_late(age_seconds, settings.alerts)
    label = f" ({status.replace('_', ' ')})" if status in ("unverified", "likely_false") else ""
    if quiet and impact < quiet_min and not late:
        # overnight bad news is often reported the next day: waiting on the phone at market open, no wake-up
        reason = f"red flag: score {impact} >= {normal_min}{label} (quiet hours: silent, {impact} < {quiet_min})"
        return Decision(True, reason, quiet_hours=True, priority="low")
    required = quiet_min if quiet and not late else normal_min
    reason = f"red flag: score {impact} >= {required}"
    if quiet:
        reason = f"quiet hours: {reason}"
    reason += label
    decision = _alert(reason, quiet, impact, late, age_seconds)
    cap = CLAIM_PRIORITY_CAP.get(status)
    if cap is not None:
        decision.priority = _cap_priority(decision.priority, cap)
    return decision


def _quiet_aware(
    reason: str,
    settings: Settings,
    now: datetime,
    impact: int,
    normal: Priority,
    age_seconds: float | None,
    cap: Priority | None = None,
) -> Decision:
    """An always-alerting decision: at least ``normal`` (higher when the impact calls for it, at most ``cap``);
    silent ("low") during quiet hours below quiet_hours_threshold."""
    quiet = is_quiet_hours(settings, now)
    late = is_late(age_seconds, settings.alerts)
    if late:
        return Decision(
            True,
            f"{reason} (late: posted {format_age(age_seconds or 0)} ago)",
            quiet_hours=quiet,
            priority=late_priority_for(impact),
        )
    if quiet and impact < int(settings.scoring.quiet_hours_threshold):
        return Decision(True, f"{reason} (quiet hours: silent)", quiet_hours=True, priority="low")
    loud = max(normal, priority_for(impact), key=_PRIORITY_ORDER.index)
    if cap is not None:
        loud = _cap_priority(loud, cap)
    return Decision(True, f"quiet hours: {reason}" if quiet else reason, quiet_hours=quiet, priority=loud)


#: claim statuses from the strongest (a confirmation) to the weakest (likely false)
CLAIM_STRENGTH: tuple[str, ...] = ("confirmed_official", "credible_report", "unverified", "likely_false")


def strongest_claim(claims: Iterable[str | None]) -> str | None:
    """The strongest claim status among ``claims`` (CLAIM_STRENGTH order), or None."""
    seen = {str(c) for c in claims if c}
    return next((c for c in CLAIM_STRENGTH if c in seen), None)


def decide_spreading(
    signal: RedFlagSignal,
    settings: Settings,
    now: datetime,
    impact: int,
    *,
    age_seconds: float | None = None,
    claims: Iterable[str | None] = (),
) -> Decision:
    """Bad news about one ticker from ``signal.distinct_authors`` accounts within the window: always alert.

    ``claims`` are the claim statuses of the posts in the window: when none is confirmed or credible, the alert is
    capped like its strongest claim (UNVERIFIED: at most "high"; LIKELY FALSE: at most "normal").
    """
    strongest = strongest_claim(claims)
    reason = (
        f"spreading: {signal.distinct_authors} accounts posted bad news about {signal.ticker} "
        f"within {signal.window_minutes} min"
    )
    if strongest in ("unverified", "likely_false"):
        reason += f" (all {strongest.replace('_', ' ')})"
    cap = CLAIM_PRIORITY_CAP.get(strongest or "")
    return _quiet_aware(reason, settings, now, int(impact), _cap_priority("high", cap or "urgent"), age_seconds, cap)


def decide_denial(
    signal: RedFlagSignal, settings: Settings, now: datetime, impact: int, *, age_seconds: float | None = None
) -> Decision:
    """A watched account denies recent red flags about a ticker: always alert ("normal")."""
    who = ", ".join(signal.denied_by) or "a watched account"
    n = len(signal.related_post_ids)
    reason = f"denial by {who} of {n} recent red flag{'s' if n != 1 else ''} about {signal.ticker}"
    return _quiet_aware(reason, settings, now, int(impact), "normal", age_seconds)


def decide_compromise(
    settings: Settings, now: datetime, *, repeat: bool = False, age_seconds: float | None = None
) -> Decision:
    """A watched account posted scam-like content: alert even in quiet hours (do not act on a fake post)."""
    quiet = is_quiet_hours(settings, now)
    reason = "possible account compromise (scam-like crypto content from a watched account)"
    if is_late(age_seconds, settings.alerts):
        late_reason = f"{reason} (late: posted {format_age(age_seconds or 0)} ago)"
        return Decision(True, late_reason, quiet_hours=quiet, priority="normal")
    if repeat:
        return Decision(True, f"{reason}; repeat within the cooldown", quiet_hours=quiet, priority="low")
    return Decision(True, reason + (" (ignores quiet hours)" if quiet else ""), quiet_hours=quiet, priority="high")


__all__ = [
    "CLAIM_PRIORITY_CAP",
    "CLAIM_STRENGTH",
    "HIGH_MIN_IMPACT",
    "URGENT_MIN_IMPACT",
    "decide",
    "decide_compromise",
    "decide_denial",
    "decide_red_flag",
    "decide_spreading",
    "format_age",
    "is_late",
    "is_late_post",
    "is_quiet_hours",
    "late_priority_for",
    "post_age_seconds",
    "priority_for",
    "red_flag_tickers",
    "strongest_claim",
]
