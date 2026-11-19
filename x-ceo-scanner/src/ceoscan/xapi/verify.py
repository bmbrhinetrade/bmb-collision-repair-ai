"""`verify`: confirm every watchlist handle is the real account and record its numeric user id.

Flow (``python -m ceoscan verify [--write] [--accept-warnings]``):

1. :func:`verify_accounts` looks every handle up with ``GET /2/users/by`` (one call per 100 handles).
2. :func:`assess_user` (pure) flags anything that looks wrong: unverified, a company account
   without the business (gold) badge, parody, protected, few followers, a display name that does
   not match ``expect_name`` (catches look-alikes), or no posts in 90 days.
3. :func:`format_verify_report` prints the result; :func:`write_user_ids` writes the user ids back
   into config/watchlist.yaml with a ruamel.yaml round-trip, so comments and layout survive.
   :func:`record_results` mirrors the results into the SQLite ``accounts`` table.

Accounts without a user id are never streamed, so an unverified handle can never trigger alerts.
"""

from __future__ import annotations

import io
import logging
import os
import re
import tempfile
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
from .client import XApiError

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

LOOKUP_BATCH = 100  # GET /2/users/by accepts at most 100 usernames
MIN_FOLLOWERS = 10_000
INACTIVE_DAYS = 90
STATUSES = ("ok", "warning", "not_found", "suspended", "error")

# Words that never count as a name match on their own ("The Official X" vs "The Real Y").
_NAME_STOPWORDS = frozenset({"the", "official", "real", "inc", "co", "corp", "llc", "ltd", "of", "and"})
_NON_WORD = re.compile(r"[\W_]+", re.UNICODE)


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

    @property
    def ok(self) -> bool:
        return self.status == "ok"


# --------------------------------------------------------------------------- assessment (pure)


def _name_tokens(text: str) -> list[str]:
    return [t for t in _NON_WORD.split(text.casefold()) if t]


def name_matches(expected: str, actual: str) -> bool:
    """Case-insensitive, punctuation-insensitive comparison; any shared word is a match.

    "Elon Musk" ~ "Elon Musk", "Michael Saylor⚡️" ~ "Michael Saylor", "OpenAI" ~ "Open AI".
    """
    exp_tokens, act_tokens = _name_tokens(expected), _name_tokens(actual)
    if not exp_tokens:
        return True  # nothing meaningful to compare against
    if not act_tokens:
        return False
    if "".join(exp_tokens) == "".join(act_tokens):  # "OpenAI" vs "Open AI"
        return True
    exp_set = {t for t in exp_tokens if t not in _NAME_STOPWORDS} or set(exp_tokens)
    act_set = {t for t in act_tokens if t not in _NAME_STOPWORDS} or set(act_tokens)
    return bool(exp_set & act_set)


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

    problems: list[str] = []
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
    if account.expect_name and name is not None and not name_matches(account.expect_name, str(name)):
        problems.append(f"display name {str(name)!r} does not match expected {account.expect_name!r}")
    if last_post_at is not None:
        age = now - last_post_at
        if age > timedelta(days=INACTIVE_DAYS):
            problems.append(f"inactive: last post {last_post_at:%Y-%m-%d} ({age.days} days ago)")
    elif tweet_count == 0:
        problems.append("inactive: account has never posted")

    return VerifyResult(
        handle=account.handle,
        status="warning" if problems else "ok",
        user_id=str(user["id"]) if user.get("id") is not None else None,
        username=str(username) if username is not None else None,
        name=str(name) if name is not None else None,
        verified=verified,
        verified_type=vtype,
        followers=followers,
        protected=protected,
        parody=parody,
        last_post_at=last_post_at,
        problems=problems,
    )


def _error_result(account: Account, error: dict) -> VerifyResult:
    """Map an entry of the /2/users/by "errors" array to not_found / suspended."""
    title = str(error.get("title") or "")
    detail = str(error.get("detail") or title or "not found")
    text = f"{title} {detail}".lower()
    status = "suspended" if "suspend" in text else "not_found"
    return VerifyResult(handle=account.handle, status=status, problems=[detail])


# --------------------------------------------------------------------------- lookup


async def verify_accounts(client: XClient, watchlist: Watchlist, now: datetime | None = None) -> list[VerifyResult]:
    """Look every watchlist handle up on X and assess it. Results are in watchlist order.

    One ``/2/users/by`` call per 100 handles. Handles X reports in ``errors`` (``"value": handle``)
    become ``not_found`` or ``suspended``; a failed request marks its batch ``error`` (never raises
    for X/network errors, so the report can still be printed).
    """
    now = now or now_utc()
    accounts = list(watchlist.accounts)
    results: list[VerifyResult] = []
    for start in range(0, len(accounts), LOOKUP_BATCH):
        batch = accounts[start : start + LOOKUP_BATCH]
        try:
            users, errors = await client.lookup_users([a.handle for a in batch], VERIFY_USER_FIELDS)
        except (XApiError, aiohttp.ClientError, TimeoutError, OSError) as exc:
            msg = str(exc) if isinstance(exc, XApiError) else f"{type(exc).__name__}: {exc}"
            logger.error("verify: user lookup failed for %d handle(s): %s", len(batch), msg)
            results.extend(
                VerifyResult(handle=a.handle, status="error", problems=[f"X API lookup failed: {msg}"]) for a in batch
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
    lines.append("Handles not found or that look wrong:")
    for r in bad:
        lines.append(f"  @{r.handle} [{_STATUS_LABEL.get(r.status, r.status)}]: {'; '.join(r.problems) or r.status}")
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
    "format_verify_report",
    "name_matches",
    "record_results",
    "verify_accounts",
    "write_user_ids",
]
