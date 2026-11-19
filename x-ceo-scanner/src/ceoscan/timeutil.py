"""Time helpers: snowflake ids, local-time formatting, quiet-hours windows."""

from __future__ import annotations

from datetime import datetime, time, timezone
from zoneinfo import ZoneInfo

# X (Twitter) snowflake epoch, milliseconds.
TWITTER_EPOCH_MS = 1288834974657


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def to_ms(dt: datetime) -> int:
    return int(dt.timestamp() * 1000)


def from_ms(ms: int) -> datetime:
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc)


def snowflake_to_datetime(post_id: str | int) -> datetime:
    """Decode the creation time (ms precision) embedded in an X snowflake id.

    X's created_at field is only second-precision; the id carries milliseconds,
    which is what we want for latency measurement.
    """
    ms = (int(post_id) >> 22) + TWITTER_EPOCH_MS
    return from_ms(ms)


def datetime_to_snowflake(dt: datetime) -> int:
    """Smallest snowflake id that could have been created at `dt`.

    Used as a `since_id` so the first poll of an account returns only posts
    created after we started, without paying for a baseline fetch.
    """
    ms = to_ms(dt) - TWITTER_EPOCH_MS
    return max(ms, 0) << 22


def parse_hhmm(value: str) -> time:
    hh, mm = value.strip().split(":")
    return time(int(hh), int(mm))


def in_window(now: datetime, start: str, end: str, tz: str) -> bool:
    """True if `now` falls in the local [start, end) window. Handles windows across midnight."""
    local = now.astimezone(ZoneInfo(tz)).time()
    s, e = parse_hhmm(start), parse_hhmm(end)
    if s == e:
        return False
    if s < e:
        return s <= local < e
    return local >= s or local < e


def local_day_start(now: datetime, tz: str) -> datetime:
    """Midnight (local tz) of the day containing `now`, returned in UTC."""
    local = now.astimezone(ZoneInfo(tz))
    midnight = local.replace(hour=0, minute=0, second=0, microsecond=0)
    return midnight.astimezone(timezone.utc)


def format_local(dt: datetime, tz: str) -> str:
    """e.g. 'Nov 19, 8:41:07 PM PT' (tz abbreviation shortened for America/* zones)."""
    local = dt.astimezone(ZoneInfo(tz))
    abbr = local.strftime("%Z")
    if abbr in ("PST", "PDT"):
        abbr = "PT"
    hour12 = local.strftime("%I").lstrip("0") or "12"
    return f"{local.strftime('%b')} {local.day}, {hour12}:{local.strftime('%M:%S %p')} {abbr}"
