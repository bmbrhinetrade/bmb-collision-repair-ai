"""`verify`: confirm every watchlist handle is the real account and record its numeric user id.

Flow (``python -m ceoscan verify [--write] [--accept-warnings]``):

1. :func:`verify_accounts` looks every handle up with ``GET /2/users/by`` (one call per 100 handles).
   A handle that is not found / suspended but has a pinned ``user_id`` is looked up by that id, so
   a renamed account is reported ("now uses @new") instead of being dropped from the watchlist.
2. :func:`assess_user` (pure) flags anything that looks wrong: a handle that now returns a
   different user id than the one pinned in the watchlist (renamed or re-registered handle),
   unverified, a company account without the business (gold) badge, parody, protected, few
   followers, an account created less than a year ago, a display name that does not contain every
   word of ``expect_name`` or that contains words like "parody", "fan" or "news", or no posts in
   90 days. The name check is a weak extra signal (anyone can copy a display name); the badge,
   follower count, account age and pinned id matter more.
3. :func:`format_verify_report` prints the result (user id changes in their own section);
   :func:`write_user_ids` writes the user ids back into config/watchlist.yaml with a ruamel.yaml
   round-trip, so comments and layout survive. :func:`record_results` mirrors the results into the
   SQLite ``accounts`` table. :func:`batch_failure` turns "every lookup failed for the same reason"
   (e.g. HTTP 402 credits depleted) into one message for the CLI.

Accounts without a user id are never streamed, so an unverified handle can never trigger alerts.
A changed user id is a warning, so it is only written with ``--accept-warnings``.
"""

from __future__ import annotations

import io
import logging
import os
import re
import tempfile
import unicodedata
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any

import aiohttp
from ruamel.yaml import YAML
from ruamel.yaml.comments import CommentedMap
from ruamel.yaml.scalarstring import DoubleQuotedScalarString
from ruamel.yaml.tokens import CommentToken

from ..config import load_watchlist
from ..models import Account, Watchlist
from ..timeutil import now_utc, snowflake_to_datetime
from .client import (
    CreditsDepleted,
    NotAuthorized,
    RateLimited,
    TooManyConnections,
    XApiError,
)

if TYPE_CHECKING:  # pragma: no cover - typing only
    from ..store import Store
    from .client import XClient

logger = logging.getLogger(__name__)

VERIFY_USER_FIELDS: list[str] = [
    "verified",
    "verified_type",
    "public_metrics",
    "description",
    "created_at",
    "protected",
    "parody",
    "most_recent_tweet_id",
]

LOOKUP_BATCH = 100  # GET /2/users/by (and GET /2/users?ids=) accept at most 100 per call
MIN_FOLLOWERS = 10_000
INACTIVE_DAYS = 90
RECENT_ACCOUNT_DAYS = 365  # a CEO account younger than this is suspicious (re-registered handle?)
STATUSES = ("ok", "warning", "not_found", "suspended", "error")

# Words that never count as a name match on their own ("The Official X" vs "The Real Y").
_NAME_STOPWORDS = frozenset({"the", "official", "real", "inc", "co", "corp", "llc", "ltd", "of", "and"})
# Display-name words that mark fan, parody and news accounts ("Elon Musk Parody Fan").
_SUSPICIOUS_NAME_WORDS = frozenset(
    {
        "parody",
        "fan",
        "fans",
        "fanclub",
        "fake",
        "satire",
        "unofficial",
        "news",
        "updates",
        "update",
        "alerts",
        "alert",
        "tracker",
        "daily",
        "archive",
        "quotes",
        "clips",
        "bot",
    }
)
_NON_WORD = re.compile(r"[\W_]+", re.UNICODE)

#: VerifyResult.error_kind values for a failed lookup request (see :func:`batch_failure`).
ERROR_KINDS = ("credits", "auth", "forbidden", "rate_limit", "network", "api")


@dataclass
class VerifyResult:
    handle: str
    status: str  # ok | warning | not_found | suspended | error
    user_id: str | None = None
    username: str | None = None
    name: str | None = None
    verified: bool | None = None
    verified_type: str | None = None
    followers: int | None = None
    protected: bool | None = None
    parody: bool | None = None
    last_post_at: datetime | None = None
    problems: list[str] = field(default_factory=list)
    #: the user_id pinned in the watchlist when X now returns a *different* id for the handle
    previous_user_id: str | None = None
    created_at: datetime | None = None
    #: why the lookup request failed (status "error"): one of ERROR_KINDS, None otherwise
    error_kind: str | None = None
    #: X's error text for a failed lookup request
    error_detail: str | None = None

    @property
    def ok(self) -> bool:
        return self.status == "ok"

    @property
    def id_changed(self) -> bool:
        """X returned a different user id than the one pinned in the watchlist."""
        return self.previous_user_id is not None and self.user_id is not None and self.user_id != self.previous_user_id


# --------------------------------------------------------------------------- assessment (pure)


def _name_tokens(text: str) -> list[str]:
    # NFKD + dropping combining marks: "José" ~ "Jose"; emoji and punctuation split into nothing.
    folded = "".join(c for c in unicodedata.normalize("NFKD", text.casefold()) if not unicodedata.combining(c))
    return [t for t in _NON_WORD.split(folded) if t]


def name_matches(expected: str, actual: str) -> bool:
    """Case-, accent- and punctuation-insensitive: every word of ``expected`` must be in ``actual``.

    "Elon Musk" ~ "Elon Musk ⚡️", "Michael Saylor⚡️" ~ "Michael Saylor", "OpenAI" ~ "Open AI";
    but "Matt Schultz" !~ "Howard Schultz" and "Sam Altman" !~ "Sam Bankman-Fried". Stopwords
    ("the", "official", "inc" ...) are ignored. Extra words in ``actual`` are allowed here; see
    :func:`suspicious_name_words` for "parody" / "fan" / "news" style additions.
    """
    exp_tokens, act_tokens = _name_tokens(expected), _name_tokens(actual)
    if not exp_tokens:
        return True  # nothing meaningful to compare against
    if not act_tokens:
        return False
    if "".join(exp_tokens) == "".join(act_tokens):  # "OpenAI" vs "Open AI"
        return True
    exp_set = {t for t in exp_tokens if t not in _NAME_STOPWORDS}
    act_set = {t for t in act_tokens if t not in _NAME_STOPWORDS}
    if not exp_set:  # only stopwords expected ("The Real"): require the same words
        return set(exp_tokens) <= set(act_tokens)
    if exp_set <= act_set:
        return True
    # Spacing differences: "Open AI" expected, "OpenAI" shown, or "Supermicro" vs "Super Micro Computer".
    # Whole words only: "Intel" never matches inside "Pintel".
    joined = "".join(t for t in exp_tokens if t not in _NAME_STOPWORDS)
    return any(
        "".join(act_tokens[i:j]) == joined for i in range(len(act_tokens)) for j in range(i + 1, len(act_tokens) + 1)
    )


def suspicious_name_words(expected: str, actual: str) -> list[str]:
    """Words like "parody", "fan" or "news" in ``actual`` that ``expected`` does not have."""
    exp = set(_name_tokens(expected))
    found = [t for t in _name_tokens(actual) if t in _SUSPICIOUS_NAME_WORDS and t not in exp]
    if "affiliated" in _name_tokens(actual) and "affiliated" not in exp:  # "not affiliated with ..."
        found.append("affiliated")
    return list(dict.fromkeys(found))


def _parse_created_at(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(str(value))  # Python 3.11+ accepts the trailing "Z"
    except ValueError:
        return None
    return dt if dt.tzinfo is not None else None


def _as_int(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def assess_user(account: Account, user: dict, now: datetime) -> VerifyResult:
    """Check one /2/users/by user object against what the watchlist expects (pure function)."""
    raw_metrics = user.get("public_metrics")
    metrics: dict[str, Any] = raw_metrics if isinstance(raw_metrics, dict) else {}
    followers = _as_int(metrics.get("followers_count"))
    tweet_count = _as_int(metrics.get("tweet_count"))
    verified = user.get("verified")
    verified = bool(verified) if verified is not None else None
    vtype_raw = user.get("verified_type")
    vtype = str(vtype_raw).lower() if vtype_raw is not None else None
    protected = user.get("protected")
    protected = bool(protected) if protected is not None else None
    parody = user.get("parody")
    parody = bool(parody) if parody is not None else None
    username = user.get("username")
    name = user.get("name")

    last_post_at: datetime | None = None
    recent_id = user.get("most_recent_tweet_id")
    if recent_id is not None and str(recent_id).isdigit():
        last_post_at = snowflake_to_datetime(str(recent_id))

    user_id = str(user["id"]) if user.get("id") is not None else None
    created_at = _parse_created_at(user.get("created_at"))

    problems: list[str] = []
    previous_user_id: str | None = None
    if account.user_id and user_id and user_id != account.user_id:
        previous_user_id = account.user_id
        problems.append(
            f"user id changed from {account.user_id} to {user_id}: @{account.handle} now belongs to a different "
            f"account (handle renamed or re-registered?). Check https://x.com/i/user/{account.user_id}"
        )
    if username and str(username).lower() != account.handle_lower:
        problems.append(f"handle resolves to @{username}, not @{account.handle}")

    is_verified = verified is True and vtype != "none"
    if verified is not True:
        problems.append("not verified")
    elif vtype == "none":
        problems.append("not verified (verified_type is none)")

    if account.kind == "company" and is_verified and vtype != "business":
        problems.append(f"company account but verified_type is {vtype or 'unknown'!r} (expected 'business' gold check)")
    if parody:
        problems.append("parody account")
    if protected:
        problems.append("protected account (posts are private)")
    if followers is not None and followers < MIN_FOLLOWERS:
        problems.append(f"low followers: {followers:,} (< {MIN_FOLLOWERS:,})")
    if created_at is not None:
        account_age = now - created_at
        if account_age < timedelta(days=RECENT_ACCOUNT_DAYS):
            problems.append(
                f"new account: created {created_at:%Y-%m-%d} ({max(account_age.days, 0)} days ago); "
                "a real CEO/company account is usually years old (re-registered handle?)"
            )
    if account.expect_name and name is not None:
        if not name_matches(account.expect_name, str(name)):
            problems.append(f"display name {str(name)!r} does not match expected {account.expect_name!r}")
        else:
            odd = suspicious_name_words(account.expect_name, str(name))
            if odd:
                problems.append(
                    f"display name {str(name)!r} contains {', '.join(repr(w) for w in odd)} "
                    "(fan, parody or news account?)"
                )
    if last_post_at is not None:
        age = now - last_post_at
        if age > timedelta(days=INACTIVE_DAYS):
            problems.append(f"inactive: last post {last_post_at:%Y-%m-%d} ({age.days} days ago)")
    elif tweet_count == 0:
        problems.append("inactive: account has never posted")

    return VerifyResult(
        handle=account.handle,
        status="warning" if problems else "ok",
        user_id=user_id,
        username=str(username) if username is not None else None,
        name=str(name) if name is not None else None,
        verified=verified,
        verified_type=vtype,
        followers=followers,
        protected=protected,
        parody=parody,
        last_post_at=last_post_at,
        problems=problems,
        previous_user_id=previous_user_id,
        created_at=created_at,
    )


def _error_result(account: Account, error: dict) -> VerifyResult:
    """Map an entry of the /2/users/by "errors" array to not_found / suspended."""
    title = str(error.get("title") or "")
    detail = str(error.get("detail") or title or "not found")
    text = f"{title} {detail}".lower()
    status = "suspended" if "suspend" in text else "not_found"
    return VerifyResult(handle=account.handle, status=status, problems=[detail])


# --------------------------------------------------------------------------- lookup


def _error_kind(exc: BaseException) -> str:
    if isinstance(exc, CreditsDepleted) or getattr(exc, "status", None) == 402:
        return "credits"
    if isinstance(exc, NotAuthorized) or getattr(exc, "status", None) in (401, 403):
        return "forbidden" if getattr(exc, "status", None) == 403 else "auth"
    if isinstance(exc, (RateLimited, TooManyConnections)) or getattr(exc, "status", None) == 429:
        return "rate_limit"
    if isinstance(exc, XApiError):
        return "api"
    return "network"


def _describe_lookup_error(exc: BaseException) -> str:
    if isinstance(exc, XApiError):
        return str(exc)
    text = " ".join(str(exc).split())
    return f"{type(exc).__name__}: {text}"[:300] if text else type(exc).__name__


_LOOKUP_ERRORS = (XApiError, aiohttp.ClientError, TimeoutError, OSError)


async def _lookup_ids(client: Any, ids: list[str]) -> tuple[list[dict], list[dict]] | None:
    """GET /2/users?ids=... (None when the client cannot do it). Raises the client's errors."""
    by_ids = getattr(client, "lookup_users_by_ids", None)
    if by_ids is not None:
        users, errors = await by_ids(ids, VERIFY_USER_FIELDS)
        return list(users), list(errors)
    request = getattr(client, "_request", None)
    if request is None:
        return None
    data = await request(
        "GET",
        "/2/users",
        "/2/users",
        params={"ids": ",".join(ids), "user.fields": ",".join(VERIFY_USER_FIELDS)},
    )
    users = data.get("data") if isinstance(data, dict) else None
    errors = data.get("errors") if isinstance(data, dict) else None
    return (users if isinstance(users, list) else []), (errors if isinstance(errors, list) else [])


async def _check_pinned_ids(client: Any, watchlist: Watchlist, results: list[VerifyResult], now: datetime) -> None:
    """For not-found / suspended handles with a pinned user_id, look the id up before it is dropped.

    A renamed account keeps its id: report it as a warning naming the new handle (the pinned id is
    kept and still streamed). Only when X confirms the id itself is gone does the result stay
    ``not_found`` / ``suspended`` (and ``write_user_ids`` removes the id). If the id lookup fails,
    the id is kept as well.
    """
    by_handle = watchlist.by_handle()
    todo = [
        (i, by_handle[r.handle.lower()])
        for i, r in enumerate(results)
        if r.status in ("not_found", "suspended")
        and r.handle.lower() in by_handle
        and by_handle[r.handle.lower()].user_id
    ]
    for start in range(0, len(todo), LOOKUP_BATCH):
        batch = todo[start : start + LOOKUP_BATCH]
        ids = [str(acct.user_id) for _, acct in batch]
        try:
            found = await _lookup_ids(client, ids)
        except _LOOKUP_ERRORS as exc:
            found = None
            failure: str | None = _describe_lookup_error(exc)
        else:
            failure = None if found is not None else "this client cannot look users up by id"
        users = {str(u.get("id")): u for u in (found[0] if found else []) if u.get("id") is not None}
        for i, acct in batch:
            old = results[i]
            user = users.get(str(acct.user_id))
            if user is not None:
                assessed = assess_user(acct, user, now)
                new_handle = assessed.username or "?"
                assessed.problems.insert(
                    0,
                    f"handle @{acct.handle} is {old.status.replace('_', ' ')}, but its pinned user_id {acct.user_id} "
                    f"now uses @{new_handle} (renamed): set handle: {new_handle} in the watchlist. "
                    "The pinned id is kept and still streamed",
                )
                assessed.problems = [p for p in assessed.problems if not p.startswith("handle resolves to @")]
                assessed.handle = old.handle
                assessed.status = "warning"
                results[i] = assessed
            elif failure is not None:
                old.problems.append(f"could not check the pinned user_id {acct.user_id} ({failure}); it is kept")
                old.status = "warning"
                old.user_id = acct.user_id


async def verify_accounts(client: XClient, watchlist: Watchlist, now: datetime | None = None) -> list[VerifyResult]:
    """Look every watchlist handle up on X and assess it. Results are in watchlist order.

    One ``/2/users/by`` call per 100 handles. Handles X reports in ``errors`` (``"value": handle``)
    become ``not_found`` or ``suspended`` (after checking a pinned user_id, see
    :func:`_check_pinned_ids`); a failed request marks its batch ``error`` with an ``error_kind``
    (never raises for X/network errors, so the report can still be printed; see
    :func:`batch_failure`).
    """
    now = now or now_utc()
    accounts = list(watchlist.accounts)
    results: list[VerifyResult] = []
    for start in range(0, len(accounts), LOOKUP_BATCH):
        batch = accounts[start : start + LOOKUP_BATCH]
        try:
            users, errors = await client.lookup_users([a.handle for a in batch], VERIFY_USER_FIELDS)
        except _LOOKUP_ERRORS as exc:
            msg = _describe_lookup_error(exc)
            kind = _error_kind(exc)
            # The CLI reports this once (batch_failure); keep the log quiet at its WARNING level.
            logger.info("verify: user lookup failed for %d handle(s): %s", len(batch), msg)
            results.extend(
                VerifyResult(
                    handle=a.handle,
                    status="error",
                    problems=[f"X API lookup failed: {msg}"],
                    error_kind=kind,
                    error_detail=msg,
                )
                for a in batch
            )
            continue

        by_name = {str(u.get("username", "")).lower(): u for u in users if u.get("username")}
        errors_by_value: dict[str, dict] = {}
        for err in errors:
            value = err.get("value") or err.get("resource_id")
            if value:
                errors_by_value.setdefault(str(value).lstrip("@").lower(), err)

        for account in batch:
            user = by_name.get(account.handle_lower)
            if user is not None:
                results.append(assess_user(account, user, now))
            elif account.handle_lower in errors_by_value:
                results.append(_error_result(account, errors_by_value[account.handle_lower]))
            else:
                results.append(
                    VerifyResult(handle=account.handle, status="error", problems=["X returned no data for this handle"])
                )
    await _check_pinned_ids(client, watchlist, results, now)
    counts = {s: sum(1 for r in results if r.status == s) for s in STATUSES}
    logger.info("verify: %d handles checked: %s", len(results), ", ".join(f"{k}={v}" for k, v in counts.items() if v))
    return results


def record_results(store: Store, results: list[VerifyResult]) -> int:
    """Mirror verify results into the SQLite ``accounts`` table (store.upsert_account). Returns rows written."""
    for r in results:
        store.upsert_account(
            handle=r.handle,
            user_id=r.user_id,
            name=r.name,
            verified=r.verified,
            verified_type=r.verified_type,
            followers=r.followers,
            status=r.status,
            problems=list(r.problems),
        )
    return len(results)


# --------------------------------------------------------------------------- batch failures

_FAILURE_TEXT = {
    "credits": "X API credits depleted (HTTP 402): add credits at console.x.com (billing).",
    "auth": "X rejected the bearer token (HTTP 401): check X_BEARER_TOKEN in .env (regenerated? stray quotes?).",
    "forbidden": "X refused the user lookup (HTTP 403): this X app may not have access to GET /2/users/by.",
    "rate_limit": "X rate limit hit on GET /2/users/by (300 requests per 15 min): wait up to 15 minutes and retry.",
    "network": "could not reach the X API",
    "api": "the X API user lookup failed",
}


def batch_failure(results: list[VerifyResult]) -> str | None:
    """One message when every lookup failed for the same reason (e.g. 402 credits), else None.

    "No handles were checked" is then true for the whole run: X bills neither failed requests nor
    anything else, and nothing should be written.
    """
    if not results or any(r.status != "error" or r.error_kind is None for r in results):
        return None
    kinds = {r.error_kind for r in results}
    if len(kinds) != 1:
        return None
    kind = kinds.pop()
    assert kind is not None
    detail = next((r.error_detail for r in results if r.error_detail), "")
    head = _FAILURE_TEXT.get(kind, _FAILURE_TEXT["api"])
    if kind in ("network", "api") and detail:
        head = f"{head} ({detail})."
    elif not head.endswith("."):
        head += "."
    return f"{head} No handles were checked, nothing was billed and nothing was written."


# --------------------------------------------------------------------------- report


def _human_count(n: int | None) -> str:
    if n is None:
        return "-"
    if n >= 1_000_000:
        return f"{n / 1_000_000:.1f}M"
    if n >= 10_000:
        return f"{n / 1_000:.0f}K"
    if n >= 1_000:
        return f"{n / 1_000:.1f}K"
    return str(n)


_STATUS_LABEL = {
    "ok": "OK",
    "warning": "WARNING",
    "not_found": "NOT FOUND",
    "suspended": "SUSPENDED",
    "error": "ERROR",
}


def format_verify_report(results: list[VerifyResult]) -> str:
    """A table of every handle plus a list of handles that were not found or look wrong."""
    counts = {s: sum(1 for r in results if r.status == s) for s in STATUSES}
    summary = ", ".join(f"{counts[s]} {s.replace('_', ' ')}" for s in STATUSES)
    lines = [f"X account verification: {len(results)} handle(s): {summary}"]
    if not results:
        return lines[0]

    rows = [("STATUS", "HANDLE", "USER ID", "NAME", "BADGE", "FOLLOWERS", "LAST POST")]
    for r in results:
        rows.append(
            (
                _STATUS_LABEL.get(r.status, r.status.upper()),
                f"@{r.handle}",
                r.user_id or "-",
                (r.name or "-")[:28],
                r.verified_type or ("-" if r.verified is None else "none"),
                _human_count(r.followers),
                f"{r.last_post_at:%Y-%m-%d}" if r.last_post_at else "-",
            )
        )
    widths = [max(len(row[i]) for row in rows) for i in range(len(rows[0]))]
    lines.append("")
    for row in rows:
        lines.append("  " + "  ".join(cell.ljust(w) for cell, w in zip(row, widths, strict=True)).rstrip())

    bad = [r for r in results if r.status != "ok"]
    lines.append("")
    if not bad:
        lines.append("All handles look like the real, verified accounts.")
        return "\n".join(lines)
    changed = [r for r in results if r.id_changed]
    if changed:
        lines.append("USER ID CHANGES - the handle now belongs to a different account than the pinned user_id:")
        for r in changed:
            lines.append(
                f"  CHANGED @{r.handle}: pinned {r.previous_user_id} -> X now returns {r.user_id} ({r.name or '-'})"
            )
        lines.append(
            "  Do not accept these unless you checked on x.com that the new account is the real one "
            "(--accept-warnings would write the new id)."
        )
        lines.append("")
    lines.append("Handles not found or that look wrong:")
    groups: dict[tuple[str, tuple[str, ...]], list[VerifyResult]] = {}
    for r in bad:
        groups.setdefault((r.status, tuple(r.problems)), []).append(r)
    for (status, problems), members in groups.items():
        label = _STATUS_LABEL.get(status, status)
        text = "; ".join(problems) or status
        if len(members) > 1 and status == "error":  # one line for "the same request failed for all of them"
            names = ", ".join(f"@{m.handle}" for m in members[:5]) + (", ..." if len(members) > 5 else "")
            lines.append(f"  {len(members)} handles [{label}]: {text} ({names})")
        else:
            lines.extend(f"  @{m.handle} [{label}]: {text}" for m in members)
    if counts["warning"]:
        lines.append(
            "Warnings are not written to the watchlist unless you pass --accept-warnings "
            "(check each account on x.com first)."
        )
    if counts["not_found"] or counts["suspended"]:
        lines.append("Not-found / suspended handles are never streamed; fix or remove them in the watchlist.")
    return "\n".join(lines)


# --------------------------------------------------------------------------- watchlist write-back


def _round_trip_yaml() -> YAML:
    yaml = YAML()  # round-trip mode: keeps comments, key order, flow style and quotes
    yaml.preserve_quotes = True
    yaml.indent(mapping=2, sequence=4, offset=2)
    yaml.width = 4096  # never re-wrap long flow sequences
    return yaml


def _post_comment(cmap: CommentedMap, key: Any) -> CommentToken | None:
    """The comment token ruamel stores after ``key``'s value (EOL comment + following comment lines)."""
    entry = cmap.ca.items.get(key)
    if entry and len(entry) > 2 and isinstance(entry[2], CommentToken):
        return entry[2]
    return None


def _split_post_comment(token: CommentToken) -> tuple[str, str]:
    """Split into (the key line's own end-of-line part, the full comment lines that follow it)."""
    head, sep, rest = token.value.partition("\n")
    return head + sep, rest


def _append_comment_lines(cmap: CommentedMap, key: Any, lines: str, like: CommentToken) -> None:
    """Emit ``lines`` (full comment/blank lines) after ``key``'s line, keeping any EOL comment it has."""
    if not lines:
        return
    entry = cmap.ca.items.setdefault(key, [None, None, None, None])
    current = entry[2] if len(entry) > 2 else None
    if isinstance(current, CommentToken):
        value = current.value
        current.value = (value if value.endswith("\n") else value + "\n") + lines
    else:
        entry[2] = CommentToken("\n" + lines, start_mark=like.start_mark, end_mark=like.end_mark)


def _insert_key(cmap: CommentedMap, pos: int, key: str, value: Any) -> None:
    """Insert ``key`` at ``pos``, keeping comment lines attached to whichever key is last.

    ruamel attaches the comment lines that follow a mapping (e.g. the "# --- next section ---"
    header of the next list item) to the mapping's last key, so they move with the "last key" role.
    """
    keys = list(cmap.keys())
    pos = max(0, min(pos, len(keys)))
    token = _post_comment(cmap, keys[-1]) if keys and pos == len(keys) else None
    cmap.insert(pos, key, value)
    if token is None:
        return
    eol, rest = _split_post_comment(token)
    if not rest:
        return
    if eol.strip():
        token.value = eol
    else:
        cmap.ca.items[keys[-1]][2] = None
    _append_comment_lines(cmap, key, rest, token)


def _delete_key(cmap: CommentedMap, key: str) -> None:
    """Delete ``key``, moving the comment lines that followed it to the previous key."""
    keys = list(cmap.keys())
    idx = keys.index(key)
    token = _post_comment(cmap, key)
    cmap.ca.items.pop(key, None)
    del cmap[key]
    if token is None or idx == 0:
        return
    _, rest = _split_post_comment(token)
    _append_comment_lines(cmap, keys[idx - 1], rest, token)


def write_user_ids(watchlist_path: Path, results: list[VerifyResult], accept_warnings: bool = False) -> int:
    """Write verified user ids into the watchlist YAML, preserving comments and layout.

    * ``ok`` (and ``warning`` when ``accept_warnings``): set ``user_id`` (inserted right after ``handle``).
      A *different* pinned id is only replaced for an accepted warning, because a changed id is
      always a warning (see :func:`assess_user`).
    * ``not_found`` / ``suspended``: remove ``user_id`` so the handle is no longer streamed.
    * ``warning`` (not accepted) / ``error``: left unchanged.

    The new file is validated with :func:`load_watchlist` and replaced atomically.
    Returns the number of accounts changed (0 -> the file is not rewritten).
    """
    path = Path(watchlist_path)
    yaml = _round_trip_yaml()
    original = path.read_text(encoding="utf-8")
    data = yaml.load(original)
    by_handle = {r.handle.lstrip("@").lower(): r for r in results}
    items = data.get("accounts") if isinstance(data, CommentedMap) else None
    changed = 0
    for item in items or []:
        if not isinstance(item, CommentedMap):
            continue
        handle = str(item.get("handle", "")).strip().lstrip("@").lower()
        r = by_handle.get(handle)
        if r is None:
            continue
        current = item.get("user_id")
        current_s = str(current).strip() if current is not None else None
        if r.status == "ok" or (r.status == "warning" and accept_warnings):
            if not r.user_id or current_s == r.user_id:
                continue
            new_value = DoubleQuotedScalarString(r.user_id)
            if "user_id" in item:
                item["user_id"] = new_value
            else:
                keys = list(item.keys())
                pos = keys.index("handle") + 1 if "handle" in keys else len(keys)
                _insert_key(item, pos, "user_id", new_value)
            changed += 1
            if current_s and current_s != r.user_id:
                logger.warning(
                    "verify: @%s user_id CHANGED %s -> %s (accepted warning)", r.handle, current_s, r.user_id
                )
            else:
                logger.info("verify: @%s user_id=%s", r.handle, r.user_id)
        elif r.status in ("not_found", "suspended") and "user_id" in item:
            _delete_key(item, "user_id")
            changed += 1
            logger.warning("verify: @%s is %s; removed its user_id", r.handle, r.status.replace("_", " "))

    if not changed:
        return 0

    buf = io.StringIO()
    yaml.dump(data, buf)
    text = buf.getvalue()
    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent))
    tmp = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
        load_watchlist(tmp)  # never leave a watchlist the service cannot load
        try:
            os.chmod(tmp, path.stat().st_mode & 0o777)
        except OSError:  # pragma: no cover - best effort
            pass
        os.replace(tmp, path)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise
    logger.info("verify: updated %d account(s) in %s", changed, path)
    return changed


__all__ = [
    "VERIFY_USER_FIELDS",
    "VerifyResult",
    "assess_user",
    "batch_failure",
    "format_verify_report",
    "name_matches",
    "record_results",
    "suspicious_name_words",
    "verify_accounts",
    "write_user_ids",
]
