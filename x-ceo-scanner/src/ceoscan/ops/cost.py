"""Monthly cost estimate printed at startup (and by `python -m ceoscan cost`).

X pay-per-use pricing (``settings.x_api.pricing``, from docs.x.com, see ``pricing.checked``):

* every post returned (each streamed post too, deduped per UTC day) is one *post read*;
* every user object returned is one *user read*;
* failed and empty responses are not billed.

Post volume comes from ``est_posts_per_day`` in the watchlist (per account and per keyword rule,
including the red-flag and source rules; a source rule without its own estimate uses the sum of its
sources' ``est_posts_per_day``), so the estimate is only as good as those numbers. ``python -m ceoscan
counts`` measures the keyword rules' real volume over the last days. A source rule with no verified
source is not streamed yet and is left out (with a note). The "worst case" adds charges for expanded
objects because X does not document whether those are billed: the referenced (quoted / replied-to)
posts, the authors of the watched accounts and keyword-rule matches, and - through the
``referenced_tweets.id.author_id`` expansion - the author of every referenced post (counted as a
distinct user each time, which is the conservative assumption).

Claude: every post from a watched account is scored when ``scoring.score_watchlist_unflagged`` is
true (the default); otherwise only the share the fast rules flag (``flag_rate``). Keyword-rule posts
always carry one of your cashtags or company names, so the fast rules flag them and they are all scored.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from ..config import Settings
from ..models import KeywordRule, Watchlist

logger = logging.getLogger(__name__)

DAYS_PER_MONTH = 30
# Worst-case assumptions (X does not document whether expansions are billed).
REFERENCED_POST_SHARE = 0.5  # share of posts that quote/reply to/repost another post
KEYWORD_AUTHORS_PER_DAY = 30  # distinct non-watchlist authors per keyword rule per day
# Every referenced post's author (expansion referenced_tweets.id.author_id) is counted as a distinct user.
TOP_LINES_SHOWN = 12
#: cost-line label per keyword rule kind
RULE_LABELS = {"keyword": "keyword rule", "red_flag": "red-flag rule", "source": "source rule"}


def rule_label(rule: KeywordRule) -> str:
    """"keyword rule big-account-cashtags", "red-flag rule red-flags", "source rule red-flag-sources (3 sources)"."""
    kind = getattr(rule, "kind", "keyword")
    label = f"{RULE_LABELS.get(kind, 'keyword rule')} {rule.name}"
    if kind == "source":
        n = len(rule.from_ids)
        label += f" ({n} source{'s' if n != 1 else ''})"
    return label


def _streamed(rule: KeywordRule) -> bool:
    """A source rule without a verified source is not streamed (build_rules skips it)."""
    return not (getattr(rule, "kind", "keyword") == "source" and not rule.from_ids)


@dataclass
class CostEstimate:
    lines: list[tuple[str, float, float]]  # (label, posts_per_month, usd_per_month)
    x_monthly: float
    x_worst_case_monthly: float
    claude_monthly: float
    one_time_verify: float
    posts_per_month: float
    cap_ok: bool
    notes: list[str]
    # Extra context for format_cost_estimate (defaults keep the spec'd constructor valid).
    mode: str = "stream"
    monthly_post_cap: int = 0
    worst_case_posts_per_month: float = 0.0

    @property
    def total_monthly(self) -> float:
        return self.x_monthly + self.claude_monthly

    @property
    def total_worst_case_monthly(self) -> float:
        return self.x_worst_case_monthly + self.claude_monthly


def estimate_monthly_cost(
    watchlist: Watchlist,
    settings: Settings,
    mode: str = "stream",
    flag_rate: float = 0.5,
    avg_input_tokens: int = 900,
    avg_output_tokens: int = 300,
) -> CostEstimate:
    """Estimate monthly X API + Claude spend from the watchlist's ``est_posts_per_day``.

    ``mode`` is "stream" or "poll" ("auto" is treated as stream). Keyword rules are handle-less
    filtered-stream rules, so they are not counted in poll mode.
    """
    pricing = settings.x_api.pricing
    poll = mode == "poll"
    if not 0.0 <= flag_rate <= 1.0:
        raise ValueError("flag_rate must be between 0 and 1")

    lines: list[tuple[str, float, float]] = []
    for a in watchlist.accounts:
        ppm = max(a.est_posts_per_day, 0.0) * DAYS_PER_MONTH
        lines.append((f"@{a.handle}", ppm, ppm * pricing.post_read))
    keyword_rules = () if poll else tuple(k for k in watchlist.keyword_rules if _streamed(k))
    for k in keyword_rules:
        ppm = max(k.est_posts_per_day, 0.0) * DAYS_PER_MONTH
        lines.append((rule_label(k), ppm, ppm * pricing.post_read))

    posts_per_month = sum(ppm for _, ppm, _ in lines)
    x_monthly = posts_per_month * pricing.post_read

    referenced_posts = posts_per_month * REFERENCED_POST_SHARE
    referenced_authors_per_day = referenced_posts / DAYS_PER_MONTH
    # A source rule's authors are its sources; other keyword rules match ~KEYWORD_AUTHORS_PER_DAY strangers.
    rule_authors = sum(
        len(k.from_ids) if getattr(k, "kind", "keyword") == "source" else KEYWORD_AUTHORS_PER_DAY
        for k in keyword_rules
    )
    authors_per_day = len(watchlist.accounts) + rule_authors + referenced_authors_per_day
    x_worst = x_monthly + referenced_posts * pricing.post_read + authors_per_day * pricing.user_read * DAYS_PER_MONTH
    worst_posts = posts_per_month + referenced_posts

    sc = settings.scoring
    per_scored = (avg_input_tokens * sc.pricing.input_per_mtok + avg_output_tokens * sc.pricing.output_per_mtok) / 1e6
    scored_per_month = 0.0
    for a in watchlist.accounts:
        ppm = max(a.est_posts_per_day, 0.0) * DAYS_PER_MONTH
        everything = sc.score_watchlist_unflagged or (a.always_alert and sc.score_always_alert_unflagged)
        scored_per_month += ppm if everything else ppm * flag_rate
    scored_per_month += sum(max(k.est_posts_per_day, 0.0) * DAYS_PER_MONTH for k in keyword_rules)
    claude_monthly = scored_per_month * per_scored if sc.enabled else 0.0

    one_time_verify = (len(watchlist.accounts) + len(watchlist.sources)) * pricing.user_read
    cap = int(pricing.monthly_post_cap)
    cap_ok = posts_per_month <= cap

    notes = [
        (
            f"Estimate uses est_posts_per_day from the watchlist and X API pricing checked {pricing.checked} "
            f"(${pricing.post_read:.3f} per post read, ${pricing.user_read:.3f} per user read)."
        ),
        "Streamed and polled posts are billed once per post per UTC day; failed or empty responses are not billed.",
        (
            f"Worst case also bills expanded objects (not documented by X): {REFERENCED_POST_SHARE:.0%} of posts "
            f"reference another post (each one's author counted as a distinct user), "
            f"~{authors_per_day:,.0f} user reads per day in all."
        ),
    ]
    if poll:
        notes.append(
            f"Poll mode: {len(watchlist.keyword_rules)} keyword rule(s) need the filtered stream and are not counted."
        )
    else:
        waiting = [k.name for k in watchlist.keyword_rules if not _streamed(k)]
        if waiting:
            notes.append(
                f"Not streamed yet, not counted: source rule(s) {', '.join(waiting)} (no source with a verified "
                "user_id; add sources and run `verify --write`)."
            )
    if sc.enabled:
        policy = (
            "every watched-account post is scored (scoring.score_watchlist_unflagged)"
            if sc.score_watchlist_unflagged
            else f"{flag_rate:.0%} of watched-account posts pass the fast rules and are scored"
        )
        notes.append(
            f"Claude estimate: {policy}, keyword-rule posts are all scored; {scored_per_month:,.0f} posts/month "
            f"by {sc.model} (~{avg_input_tokens} input / {avg_output_tokens} output tokens at "
            f"${sc.pricing.input_per_mtok:.2f} / ${sc.pricing.output_per_mtok:.2f} per million tokens)."
        )
    else:
        notes.append("Claude scoring is disabled (scoring.enabled=false): no Claude cost.")
    if cap_ok:
        if worst_posts > cap:
            notes.append(f"Worst case ({worst_posts:,.0f} post reads/month) would exceed the {cap:,} monthly cap.")
    else:
        notes.append(
            f"WARNING: {posts_per_month:,.0f} posts/month exceeds the {cap:,} monthly post-read cap; "
            "reduce accounts, set include_replies: false, or tighten keyword rules."
        )
    missing_ids = sum(1 for a in watchlist.accounts if not a.user_id)
    if missing_ids:
        notes.append(
            f"{missing_ids} account(s) have no user_id yet (run `verify --write`); they are included in the estimate."
        )
    missing_sources = sum(1 for src in watchlist.sources if not src.user_id)
    if missing_sources:
        notes.append(
            f"{missing_sources} source(s) have no user_id yet (run `verify --write`); they are not streamed until then."
        )
    if not poll and any(getattr(k, "kind", "keyword") != "keyword" for k in keyword_rules):
        notes.append(
            "Red-flag and source rule volumes are watchlist estimates; `python -m ceoscan counts` measures the "
            f"real volume of every keyword rule over the last 7 days (read-only, ${pricing.counts_request:.3f} "
            "per request)."
        )
    if x_monthly > 0 and lines:
        label, ppm, usd = max(lines, key=lambda line: line[2])
        share = usd / x_monthly
        if share >= 0.3:
            hint = ""
            acct = watchlist.by_handle().get(label.lstrip("@").lower()) if label.startswith("@") else None
            if acct is not None and acct.include_replies:
                hint = " (include_replies: false would cut it)"
            notes.append(f"Largest source: {label} at {share:.0%} of the X cost{hint}.")

    est = CostEstimate(
        lines=lines,
        x_monthly=x_monthly,
        x_worst_case_monthly=x_worst,
        claude_monthly=claude_monthly,
        one_time_verify=one_time_verify,
        posts_per_month=posts_per_month,
        cap_ok=cap_ok,
        notes=notes,
        mode="poll" if poll else "stream",
        monthly_post_cap=cap,
        worst_case_posts_per_month=worst_posts,
    )
    logger.debug(
        "cost estimate: %.0f posts/month, X $%.2f (worst $%.2f), Claude $%.2f",
        posts_per_month,
        x_monthly,
        x_worst,
        claude_monthly,
    )
    return est


def _usd(v: float) -> str:
    return f"${v:,.2f}"


def format_cost_estimate(est: CostEstimate) -> str:
    """Human-readable multi-line cost report."""
    cap_text = f"the {est.monthly_post_cap:,} monthly cap" if est.monthly_post_cap else "the monthly cap"
    cap_state = f"within {cap_text}" if est.cap_ok else f"OVER {cap_text}"
    out = [
        f"Estimated monthly cost ({est.mode} mode)",
        f"  Posts read from X:   {est.posts_per_month:,.0f} / month ({cap_state})",
        f"  X API post reads:    {_usd(est.x_monthly)} / month",
        f"  X API worst case:    {_usd(est.x_worst_case_monthly)} / month (if expansions are billed)",
        f"  Claude scoring:      {_usd(est.claude_monthly)} / month",
        f"  Total:               {_usd(est.total_monthly)} / month (worst case {_usd(est.total_worst_case_monthly)})",
        f"  One-time verify:     {_usd(est.one_time_verify)} (user lookups)",
    ]
    if est.lines:
        out.append("  By source:")
        ranked = sorted(est.lines, key=lambda line: (-line[2], line[0]))
        width = max(len(label) for label, _, _ in ranked[:TOP_LINES_SHOWN])
        for label, ppm, usd in ranked[:TOP_LINES_SHOWN]:
            out.append(f"    {label:<{width}}  {ppm:>9,.0f} posts/mo  {_usd(usd):>9}/mo")
        rest = ranked[TOP_LINES_SHOWN:]
        if rest:
            out.append(
                f"    ... {len(rest)} more: {sum(p for _, p, _ in rest):,.0f} posts/mo, "
                f"{_usd(sum(u for _, _, u in rest))}/mo"
            )
    for note in est.notes:
        out.append(f"  * {note}")
    return "\n".join(out)
