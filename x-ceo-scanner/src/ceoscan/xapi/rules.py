"""Filtered-stream rule building and syncing.

* :func:`build_rules` turns the watchlist into X filtered-stream rules: account rules
  ``(from:ID1 OR from:ID2 ...) -is:retweet -is:reply`` packed greedily under the
  per-rule length limit, plus keyword rules
  ``[(from:ID OR ...)] ($TSLA OR $NVDA OR Tesla ...) [(fraud OR lawsuit ...)] [min_followers:N] <extra>``.
  Only accounts and sources with a verified numeric ``user_id`` are streamed - handles are never used.
  Keyword rules come in three kinds: ``keyword`` (cashtags / words from big accounts), ``red_flag``
  (my tickers or company names AND a red-flag word such as "short report" or lawsuit) and ``source``
  (posts about my tickers from the watchlist's ``sources``: short sellers, regulators, news wires).
  A rule that does not fit in one stream rule is split in two dimensions (author ids x terms).
* :func:`build_count_queries` renders the same keyword rules for ``GET /2/tweets/counts/recent``
  (512 characters per query, without operators that endpoint rejects, such as ``min_followers``).
* :func:`plan_sync` diffs desired rules against the rules already on the app.
* :func:`sync_rules` validates additions with ``dry_run=true`` first, then deletes and adds.

All functions except :func:`sync_rules` are pure and deterministic (watchlist order).
"""

from __future__ import annotations

import logging
import re
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field, replace
from typing import TYPE_CHECKING, Any

from ..config import RulesSettings
from ..models import KeywordRule, Watchlist
from .client import COUNTS_QUERY_MAX

if TYPE_CHECKING:  # pragma: no cover - typing only
    from .client import XClient

log = logging.getLogger(__name__)

# Below this a cashtag keyword rule can match an enormous (and expensive) volume of posts.
LOW_MIN_FOLLOWERS_WARNING = 10_000
#: Stream-only operators: the search / counts endpoints reject them ("Unrecognized operator:'min_followers'",
#: seen 2026-11-25), so `counts` leaves them out of its queries and says so. The user-metric and sampling
#: operators are listed for the filtered stream only (docs.x.com filtered-stream operators).
COUNTS_UNSUPPORTED_OPERATORS = frozenset(
    {"min_followers", "followers_count", "tweets_count", "following_count", "listed_count", "sample"}
)
# One operator token of extra_operators: op:"quoted value" or a run of non-spaces (linear time).
_OPERATOR_TOKEN_RE = re.compile(r'-?[A-Za-z_]+:"[^"]{0,200}"|\S+')


@dataclass(frozen=True)
class RuleSpec:
    value: str
    tag: str


@dataclass
class RuleBuildResult:
    rules: list[RuleSpec]
    skipped_accounts: list[str]  # handles without a user_id (not streamed)
    warnings: list[str] = field(default_factory=list)
    skipped_sources: list[str] = field(default_factory=list)  # source handles without a user_id


@dataclass(frozen=True)
class CountQuery:
    """One ``GET /2/tweets/counts/recent`` query for (a chunk of) one keyword rule."""

    rule: str  # keyword rule name
    kind: str  # keyword | red_flag | source
    n: int  # 1-based chunk number within the rule
    query: str  # <= COUNTS_QUERY_MAX chars
    removed: tuple[str, ...] = ()  # operators left out because the counts endpoint rejects them


@dataclass
class RuleSyncPlan:
    to_add: list[RuleSpec]
    to_delete: list[dict]  # existing X rule objects ({"id", "value", "tag"})
    unchanged: list[dict]
    foreign: list[dict]  # rules this app did not create (deleted only if manage_foreign)


# --------------------------------------------------------------------------- building


def account_suffix(include_replies: bool, include_retweets: bool) -> str:
    """Operators appended to an account rule: "-is:retweet" / "-is:reply" unless included."""
    parts: list[str] = []
    if not include_retweets:
        parts.append("-is:retweet")
    if not include_replies:
        parts.append("-is:reply")
    return " ".join(parts)


def _or_group(terms: Sequence[str]) -> str:
    """``(a OR b OR c)``; a single term is written bare (always valid X syntax)."""
    if len(terms) == 1:
        return terms[0]
    return "(" + " OR ".join(terms) + ")"


def _with_tail(group: str, tail: str) -> str:
    return f"{group} {tail}" if tail else group


def _pack(terms: Sequence[str], render: Callable[[Sequence[str]], str], max_len: int, what: str) -> list[list[str]]:
    """Greedily pack ``terms`` (in order) into chunks whose rendered rule is <= max_len chars."""
    chunks: list[list[str]] = []
    current: list[str] = []
    for term in terms:
        single = render([term])
        if len(single) > max_len:
            raise ValueError(f"{what}: {single!r} is {len(single)} chars and cannot fit in one rule (max {max_len})")
        if current and len(render([*current, term])) > max_len:
            chunks.append(current)
            current = [term]
        else:
            current.append(term)
    if current:
        chunks.append(current)
    return chunks


def _normalize_ops(ops: str) -> str:
    return " ".join((ops or "").split())


def render_term(term: str) -> str:
    """One keyword-rule term in X rule syntax.

    ``$apmd`` -> ``$APMD`` (cashtag); a single word made of letters/digits/underscores is written
    bare (X matches it case-insensitively as a whole word); anything else (spaces, hyphens, dots)
    becomes an exact phrase in double quotes. Terms are validated in config.py (no quotes, parens).
    """
    t = " ".join((term or "").split())
    if not t:
        return ""
    if t.startswith("$"):
        return "$" + t[1:].upper()
    if t.replace("_", "").isalnum():
        return t
    return f'"{t}"'


def keyword_items(kw: KeywordRule) -> list[str]:
    """The rule's first OR-group: its tickers as $CASHTAGS, then its terms (deduplicated, in order)."""
    tickers = list(dict.fromkeys(t.strip().upper().lstrip("$") for t in kw.tickers if t and t.strip()))
    items = [f"${t}" for t in tickers]
    for term in getattr(kw, "terms", ()):
        rendered = render_term(term)
        if rendered and rendered not in items:
            items.append(rendered)
    return items


def _or_len(lengths: Sequence[int]) -> int:
    """len(_or_group(terms)) from the terms' lengths."""
    if len(lengths) == 1:
        return lengths[0]
    return sum(lengths) + 4 * (len(lengths) - 1) + 2


def _greedy(units: Sequence[str], budget: int, overhead: int) -> list[list[str]] | None:
    """Pack ``units`` in order so that every chunk's OR-group plus ``overhead`` is <= ``budget``.

    None when a single unit does not fit.
    """
    chunks: list[list[str]] = []
    current: list[int] = []
    current_units: list[str] = []
    for unit in units:
        if len(unit) + overhead > budget:
            return None
        if current and _or_len([*current, len(unit)]) + overhead > budget:
            chunks.append(current_units)
            current, current_units = [], []
        current.append(len(unit))
        current_units.append(unit)
    if current_units:
        chunks.append(current_units)
    return chunks


def keyword_rule_values(kw: KeywordRule, max_len: int) -> list[str]:
    """Render one keyword rule as stream rule values of at most ``max_len`` characters.

    ``[(from:ID OR ...)] (<cashtags and terms>) [(<require_any>)] [min_followers:N] <extra_operators>``.
    min_followers is left out when it is 0. A rule that does not fit in one value is split over the
    author ids and over the cashtags/terms (never over ``require_any``, which every value carries),
    choosing the split that needs the fewest values. Without author ids the cashtags/terms are packed
    greedily, exactly as before sources existed (so existing rules stay byte-identical).
    Raises ValueError when even one author id and one term cannot fit.
    """
    items = keyword_items(kw)
    if not items:
        return []
    require = list(dict.fromkeys(r for r in (render_term(t) for t in getattr(kw, "require_any", ())) if r))
    tail_parts = [f"min_followers:{int(kw.min_followers)}"] if kw.min_followers > 0 else []
    extra = _normalize_ops(kw.extra_operators)
    if extra:
        tail_parts.append(extra)
    rest = " ".join(([_or_group(require)] if require else []) + tail_parts)
    what = f"keyword rule {kw.name!r}"

    def render(ids: Sequence[str], chunk: Sequence[str]) -> str:
        parts = [_or_group([f"from:{i}" for i in ids])] if ids else []
        parts.append(_or_group(chunk))
        if rest:
            parts.append(rest)
        return " ".join(parts)

    ids = list(dict.fromkeys(str(i).strip() for i in getattr(kw, "from_ids", ()) if str(i).strip()))
    if not ids:
        return [render((), chunk) for chunk in _pack(items, lambda c: render((), c), max_len, what)]

    from_terms = [f"from:{i}" for i in ids]
    item_overhead = len(rest) + 1 if rest else 0
    best: tuple[tuple[int, int, int], list[list[str]], list[list[str]]] | None = None
    # b = room for the "(from:...) " part; the cashtags/terms part gets the rest of max_len.
    for b in range(min(len(t) for t in from_terms) + 1, max_len + 1):
        id_chunks = _greedy(from_terms, b, 1)
        item_chunks = _greedy(items, max_len - b, item_overhead) if id_chunks is not None else None
        if id_chunks is None or item_chunks is None:
            continue
        score = (len(id_chunks) * len(item_chunks), len(item_chunks), b)
        if best is None or score < best[0]:
            best = (score, id_chunks, item_chunks)
    if best is None:
        raise ValueError(
            f"{what}: one author id and one term (with the red-flag group and operators) cannot fit in one rule"
            f" (max {max_len} chars); shorten require_any or extra_operators"
        )
    _, id_chunks, item_chunks = best
    values = [
        render([t[len("from:"):] for t in id_chunk], item_chunk) for id_chunk in id_chunks for item_chunk in item_chunks
    ]
    for value in values:  # the arithmetic above is exact; never send an over-long rule if it ever is not
        if len(value) > max_len:
            raise ValueError(f"{what}: rendered rule is {len(value)} chars (max {max_len})")
    return values


def strip_counts_operators(ops: str) -> tuple[str, list[str]]:
    """Split ``extra_operators`` into (what the counts endpoint accepts, the stream-only operators removed)."""
    kept: list[str] = []
    removed: list[str] = []
    for token in _OPERATOR_TOKEN_RE.findall(ops or ""):
        name = token.lstrip("-").split(":", 1)[0].lower() if ":" in token else ""
        (removed if name in COUNTS_UNSUPPORTED_OPERATORS else kept).append(token)
    return " ".join(kept), removed


def remove_operator(query: str, operator: str) -> str:
    """``query`` without any ``operator:value`` / ``-operator:value`` token (used after X rejects one)."""
    op = operator.lower()
    kept = [
        t
        for t in _OPERATOR_TOKEN_RE.findall(query)
        if not (":" in t and t.lstrip("-").split(":", 1)[0].lower() == op)
    ]
    return " ".join(kept)


def _split_even(terms: Sequence[str], parts: int) -> list[list[str]]:
    """``terms`` in order, cut into ``parts`` contiguous pieces of similar rendered length."""
    total = sum(len(t) + 4 for t in terms)
    pieces: list[list[str]] = [[]]
    used = 0
    for t in terms:
        if pieces[-1] and used >= total * len(pieces) / parts and len(pieces) < parts:
            pieces.append([])
        pieces[-1].append(t)
        used += len(t) + 4
    return pieces


def _count_values(kw: KeywordRule, max_len: int) -> list[str]:
    """:func:`keyword_rule_values` for a counts query; ``require_any`` may be split too if it is too long.

    ``A (B1 OR B2)`` matches the same posts as ``A B1`` OR ``A B2``, so splitting the red-flag group is
    exact for the set of posts, and the summed counts stay an upper bound (a post can match both).
    The split with the fewest queries wins.
    """
    try:
        return keyword_rule_values(kw, max_len)
    except ValueError:
        require = list(getattr(kw, "require_any", ()))
        if len(require) < 2:
            raise
    best: list[str] | None = None
    for parts in range(2, len(require) + 1):
        try:
            values = [
                v
                for piece in _split_even(require, parts)
                for v in keyword_rule_values(replace(kw, require_any=tuple(piece)), max_len)
            ]
        except ValueError:
            continue
        if best is None or len(values) < len(best):
            best = values
    if best is None:
        raise ValueError(f"keyword rule {kw.name!r} cannot be split into {max_len}-character queries")
    return best


def build_count_queries(
    watchlist: Watchlist, settings: RulesSettings, max_len: int = COUNTS_QUERY_MAX
) -> tuple[list[CountQuery], list[str]]:
    """Every keyword rule rendered as ``GET /2/tweets/counts/recent`` queries (read-only volume check).

    The counts endpoint takes at most ``max_len`` (512) characters and rejects stream-only operators
    such as ``min_followers``, so those are left out (and listed in ``CountQuery.removed``) and the rule
    is re-packed for the shorter limit (a long ``require_any`` group is split as well). Counts are therefore an upper bound for rules that use
    min_followers. Source rules without a verified source are skipped (nothing to count yet).
    Returns (queries, notes).
    """
    queries: list[CountQuery] = []
    notes: list[str] = []
    for kw in watchlist.keyword_rules:
        kind = getattr(kw, "kind", "keyword")
        if kind == "source" and not kw.from_ids:
            notes.append(f"{kw.name}: skipped - no source with a verified user_id yet (nothing to count)")
            continue
        extra, removed = strip_counts_operators(kw.extra_operators)
        if kw.min_followers > 0:
            removed.insert(0, f"min_followers:{int(kw.min_followers)}")
        try:
            values = _count_values(replace(kw, min_followers=0, extra_operators=extra), max_len)
        except ValueError as exc:
            notes.append(f"{kw.name}: skipped - {exc}")
            continue
        if not values:
            notes.append(f"{kw.name}: skipped - no tickers or terms")
            continue
        queries.extend(
            CountQuery(rule=kw.name, kind=kind, n=n, query=v, removed=tuple(removed))
            for n, v in enumerate(values, start=1)
        )
    return queries, notes


def build_rules(watchlist: Watchlist, settings: RulesSettings) -> RuleBuildResult:
    """Build every filtered-stream rule for ``watchlist``.

    Raises ValueError if more than ``settings.max_rules`` rules are needed or a single
    account / ticker cannot fit in one rule of ``settings.max_rule_length`` chars.
    """
    prefix = settings.tag_prefix
    max_len = int(settings.max_rule_length)
    rules: list[RuleSpec] = []
    skipped: list[str] = []
    warnings: list[str] = []
    seen_values: set[str] = set()

    def emit(value: str, tag: str) -> None:
        if value in seen_values:  # X rejects duplicate rule values
            warnings.append(f"duplicate rule value skipped (tag {tag}): {value}")
            return
        seen_values.add(value)
        rules.append(RuleSpec(value=value, tag=tag))

    # ---- account rules: group by (include_replies, include_retweets), watchlist order
    groups: dict[tuple[bool, bool], list[str]] = {}
    id_owner: dict[str, str] = {}
    for account in watchlist.accounts:
        if not account.user_id:
            skipped.append(account.handle)
            continue
        uid = str(account.user_id).strip()
        if uid in id_owner:
            warnings.append(
                f"@{account.handle} has the same user_id {uid} as @{id_owner[uid]}; streaming it once"
                f" with @{id_owner[uid]}'s reply/retweet settings"
            )
            continue
        id_owner[uid] = account.handle
        groups.setdefault((account.include_replies, account.include_retweets), []).append(uid)

    if skipped:
        warnings.append(
            f"{len(skipped)} account(s) have no verified user_id and will NOT be streamed"
            f" (run `python -m ceoscan verify --write`): {', '.join('@' + h for h in skipped)}"
        )

    for (replies, retweets), ids in groups.items():
        suffix = account_suffix(replies, retweets)

        def render_accounts(chunk: Sequence[str], _suffix: str = suffix) -> str:
            return _with_tail(_or_group([f"from:{i}" for i in chunk]), _suffix)

        chunks = _pack(ids, render_accounts, max_len, "account rule")
        for n, chunk in enumerate(chunks, start=1):
            emit(render_accounts(chunk), f"{prefix}:acct:r{int(replies)}t{int(retweets)}:{n}")

    # ---- sources: accounts watched only for posts about my tickers (kind "source" keyword rules)
    skipped_sources = [s.handle for s in watchlist.sources if not s.user_id]
    if skipped_sources:
        warnings.append(
            f"{len(skipped_sources)} source(s) have no verified user_id: they are NOT streamed and not recognized"
            f" as sources (run `python -m ceoscan verify --write`): {', '.join('@' + h for h in skipped_sources)}"
        )

    # ---- keyword rules: cashtags / words / phrases, optionally AND red-flag words, optionally from sources
    for kw in watchlist.keyword_rules:
        kind = getattr(kw, "kind", "keyword")
        if not keyword_items(kw):
            warnings.append(f"keyword rule {kw.name!r} has no tickers or terms; skipped")
            continue
        if kind == "source" and not kw.from_ids:
            warnings.append(
                f"keyword rule {kw.name!r} (kind source) has no source with a verified user_id yet; skipped"
                " (add sources to the watchlist and run `python -m ceoscan verify --write`)"
            )
            continue
        if kind == "red_flag" and kw.min_followers < LOW_MIN_FOLLOWERS_WARNING:
            warnings.append(
                f"red-flag rule {kw.name!r} uses min_followers:{kw.min_followers} (< {LOW_MIN_FOLLOWERS_WARNING:,};"
                f" watchlist estimate: {kw.est_posts_per_day:g} posts/day): anyone can post a bad-news word next to"
                " a ticker, so a low threshold matches a lot of noise (and cost) - check `python -m ceoscan counts`"
                " and the daily summary's post count"
            )
        elif kind == "keyword" and not kw.from_ids and kw.min_followers < LOW_MIN_FOLLOWERS_WARNING:
            warnings.append(
                f"keyword rule {kw.name!r} uses min_followers:{kw.min_followers} (watchlist estimate:"
                f" {kw.est_posts_per_day:g} posts/day); a low threshold with common words can match a large"
                " (and costly) volume of posts - check the daily summary's post count"
            )
        for n, value in enumerate(keyword_rule_values(kw, max_len), start=1):
            emit(value, f"{prefix}:kw:{kw.name}:{n}")

    if len(rules) > settings.max_rules:
        raise ValueError(
            f"{len(rules)} stream rules are needed but the plan allows {settings.max_rules};"
            " remove accounts or keyword tickers"
        )
    if not rules:
        warnings.append("no stream rules: no account has a verified user_id and there are no keyword rules")
    return RuleBuildResult(rules=rules, skipped_accounts=skipped, warnings=warnings, skipped_sources=skipped_sources)


# --------------------------------------------------------------------------- syncing


def _is_ours(rule: dict, marker: str) -> bool:
    tag = rule.get("tag")
    return isinstance(tag, str) and tag.startswith(marker)


def plan_sync(desired: list[RuleSpec], existing: list[dict], tag_prefix: str, manage_foreign: bool) -> RuleSyncPlan:
    """Diff desired rules against the app's existing rules (matched by exact value).

    * existing rules tagged ``"<tag_prefix>:..."`` are ours; ours not desired -> to_delete
    * desired values not present -> to_add (duplicate desired values are added once)
    * other rules are foreign; they go to to_delete only when ``manage_foreign``.
      A foreign rule whose value equals a desired rule already does that rule's job (X
      rejects duplicate values), so it is reported as unchanged instead.
    """
    marker = f"{tag_prefix}:"
    # value -> indices into existing; our own rules are preferred over foreign ones
    candidates: dict[str, list[int]] = {}
    for ours_pass in (True, False):
        for i, rule in enumerate(existing):
            if _is_ours(rule, marker) == ours_pass and isinstance(rule.get("value"), str):
                candidates.setdefault(rule["value"], []).append(i)

    used: set[int] = set()
    to_add: list[RuleSpec] = []
    unchanged: list[dict] = []
    seen: set[str] = set()
    for spec in desired:
        if spec.value in seen:
            continue
        seen.add(spec.value)
        match = next((i for i in candidates.get(spec.value, []) if i not in used), None)
        if match is None:
            to_add.append(spec)
        else:
            used.add(match)
            unchanged.append(existing[match])

    stale_ours = [r for i, r in enumerate(existing) if i not in used and _is_ours(r, marker)]
    foreign = [r for i, r in enumerate(existing) if i not in used and not _is_ours(r, marker)]
    to_delete = stale_ours + (foreign if manage_foreign else [])
    return RuleSyncPlan(to_add=to_add, to_delete=to_delete, unchanged=unchanged, foreign=foreign)


def _to_int(value: Any) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def rule_errors(resp: dict) -> tuple[list[str], list[str]]:
    """Split a rules-endpoint response into (fatal problems, benign duplicate notes)."""
    problems: list[str] = []
    duplicates: list[str] = []
    for err in resp.get("errors") or []:
        if not isinstance(err, dict):
            continue
        title = str(err.get("title") or "error")
        msg = title
        if err.get("value"):
            msg += f" for rule {err['value']!r}"
        details = err.get("details")
        if isinstance(details, str):
            details = [details]
        extra = "; ".join(str(d) for d in details or []) or err.get("detail") or err.get("message")
        if extra:
            msg += f": {extra}"
        (duplicates if title == "DuplicateRule" else problems).append(msg)
    summary = (resp.get("meta") or {}).get("summary") or {}
    invalid = _to_int(summary.get("invalid"))
    if invalid and not problems:
        problems.append(f"X reported {invalid} invalid rule(s)")
    return problems, duplicates


async def sync_rules(
    client: XClient, desired: list[RuleSpec], settings: RulesSettings, apply: bool = True
) -> RuleSyncPlan:
    """Make the app's stream rules match ``desired``.

    Validates every addition with ``dry_run=true`` first and raises ValueError (with X's
    error details) if any is invalid - before anything is changed. If ``apply``: delete
    stale rules, then add new ones. Returns the plan.
    """
    existing = await client.get_rules()
    plan = plan_sync(desired, existing, settings.tag_prefix, settings.manage_foreign_rules)
    log.info(
        "stream rules: %d desired, %d unchanged, %d to add, %d to delete, %d foreign%s",
        len(desired),
        len(plan.unchanged),
        len(plan.to_add),
        len(plan.to_delete),
        len(plan.foreign),
        " (will delete)" if settings.manage_foreign_rules else " (kept)",
    )

    if plan.to_add:
        resp = await client.add_rules(plan.to_add, dry_run=True)
        problems, duplicates = rule_errors(resp)
        if problems:
            raise ValueError("X rejected stream rule(s) in validation (dry run): " + " | ".join(problems))
        for note in duplicates:
            log.warning("stream rule already exists: %s", note)

    if not apply:
        return plan

    if plan.to_delete:
        ids = [str(r["id"]) for r in plan.to_delete if r.get("id")]
        resp = await client.delete_rules(ids)
        problems, _ = rule_errors(resp)
        for problem in problems:
            log.warning("stream rule delete problem: %s", problem)
        summary = (resp.get("meta") or {}).get("summary") or {}
        log.info("deleted %d stream rule(s)", _to_int(summary.get("deleted", len(ids))))

    if plan.to_add:
        resp = await client.add_rules(plan.to_add)
        problems, duplicates = rule_errors(resp)
        if problems:
            raise ValueError("X rejected stream rule(s): " + " | ".join(problems))
        for note in duplicates:
            log.warning("stream rule already exists: %s", note)
        summary = (resp.get("meta") or {}).get("summary") or {}
        log.info("added %d stream rule(s)", _to_int(summary.get("created", len(plan.to_add))))
    return plan
