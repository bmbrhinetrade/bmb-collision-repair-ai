"""Filtered-stream rule building and syncing.

* :func:`build_rules` turns the watchlist into X filtered-stream rules: account rules
  ``(from:ID1 OR from:ID2 ...) -is:retweet -is:reply`` packed greedily under the
  per-rule length limit, plus keyword rules ``($TSLA OR $NVDA ...) min_followers:N <extra>``.
  Only accounts with a verified numeric ``user_id`` are streamed - handles are never used.
* :func:`plan_sync` diffs desired rules against the rules already on the app.
* :func:`sync_rules` validates additions with ``dry_run=true`` first, then deletes and adds.

All functions except :func:`sync_rules` are pure and deterministic (watchlist order).
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from ..config import RulesSettings
from ..models import Watchlist

if TYPE_CHECKING:  # pragma: no cover - typing only
    from .client import XClient

log = logging.getLogger(__name__)

# Below this a cashtag keyword rule can match an enormous (and expensive) volume of posts.
LOW_MIN_FOLLOWERS_WARNING = 10_000


@dataclass(frozen=True)
class RuleSpec:
    value: str
    tag: str


@dataclass
class RuleBuildResult:
    rules: list[RuleSpec]
    skipped_accounts: list[str]  # handles without a user_id (not streamed)
    warnings: list[str] = field(default_factory=list)


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

    # ---- keyword rules: cashtags from large accounts
    for kw in watchlist.keyword_rules:
        tickers = list(dict.fromkeys(t.strip().upper().lstrip("$") for t in kw.tickers if t and t.strip()))
        if not tickers:
            warnings.append(f"keyword rule {kw.name!r} has no tickers; skipped")
            continue
        if kw.min_followers < LOW_MIN_FOLLOWERS_WARNING:
            warnings.append(
                f"keyword rule {kw.name!r} uses min_followers:{kw.min_followers}; this can match a very"
                " large (and costly) volume of posts"
            )
        tail = _with_tail(f"min_followers:{int(kw.min_followers)}", _normalize_ops(kw.extra_operators))

        def render_keywords(chunk: Sequence[str], _tail: str = tail) -> str:
            return _with_tail(_or_group([f"${t}" for t in chunk]), _tail)

        chunks = _pack(tickers, render_keywords, max_len, f"keyword rule {kw.name!r}")
        for n, chunk in enumerate(chunks, start=1):
            emit(render_keywords(chunk), f"{prefix}:kw:{kw.name}:{n}")

    if len(rules) > settings.max_rules:
        raise ValueError(
            f"{len(rules)} stream rules are needed but the plan allows {settings.max_rules};"
            " remove accounts or keyword tickers"
        )
    if not rules:
        warnings.append("no stream rules: no account has a verified user_id and there are no keyword rules")
    return RuleBuildResult(rules=rules, skipped_accounts=skipped, warnings=warnings)


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
