"""Replay saved stream-shaped posts through the full pipeline without touching X.

``python -m ceoscan replay samples/sample_posts.jsonl`` feeds each JSONL line to
:meth:`Pipeline.process` exactly as the stream would (dedupe, SQLite, fast rules, scoring,
decision, formatting, dispatch), so the whole path can be exercised while the X account has no
credits. Defaults are safe: an in-memory database, the offline fast-rules scorer and a console
"notifier". ``--claude`` scores with Claude, ``--send`` pushes to the real alert channels; replayed
alerts are always marked ``[REPLAY]`` so they cannot be mistaken for live posts.

With ``rebase_times`` (default) each sample's post id is rewritten to a fresh snowflake for
"now - x_delay_seconds" right before it is processed, so the measured latency is meaningful.
The same original id always maps to the same new id, so duplicate samples still dedupe.
A sample may carry a top-level ``"replay_age_seconds": 7200`` to be rebased that far in the past
instead, which replays an outage catch-up (a late post: low-key "Missed during outage" alert).
Late alerts are reported separately and left out of the replay's latency summary, like in /health.
"""

from __future__ import annotations

import copy
import dataclasses
import json
import logging
from collections.abc import Callable
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import aiohttp

from .alerts.decision import is_quiet_hours
from .alerts.dispatcher import AlertDispatcher, build_dispatcher
from .alerts.notifiers import ConsoleNotifier
from .config import AppConfig
from .http import new_session
from .models import AlertMessage, AlertRecord, ChannelResult, IngestItem, IngestStatus
from .pipeline import HandleResult, Pipeline, Scorer
from .scoring.claude import ClaudeScorer, OfflineScorer
from .store import Store
from .timeutil import datetime_to_snowflake, now_utc, snowflake_to_datetime

logger = logging.getLogger(__name__)

COMMENT_PREFIX = "//"
REPLAY_TAG = "[REPLAY]"
REPLAY_NOTE = "(replay of a saved sample - not a live post)"
#: optional top-level sample key: rebase the post this many seconds before it is "received"
AGE_KEY = "replay_age_seconds"


# --------------------------------------------------------------------------- samples


def load_samples(path: str | Path) -> list[dict]:
    """Read a JSONL file of stream-shaped payloads; blank lines and ``//`` comment lines are skipped."""
    p = Path(path)
    samples: list[dict] = []
    with p.open(encoding="utf-8-sig") as fh:
        for lineno, line in enumerate(fh, 1):
            s = line.strip()
            if not s or s.startswith(COMMENT_PREFIX):
                continue
            try:
                obj = json.loads(s)
            except json.JSONDecodeError as exc:
                raise ValueError(f"{p}:{lineno}: invalid JSON ({exc.msg})") from None
            if not isinstance(obj, dict):
                # ValueError like the JSON error above, so callers handle "bad sample file" in one place
                raise ValueError(f"{p}:{lineno}: expected a JSON object, got {type(obj).__name__}")  # noqa: TRY004
            samples.append(obj)
    return samples


def _id_of(value: Any) -> str | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return str(value)
    if isinstance(value, str) and value.isascii() and value.isdigit():
        return value
    return None


def _iso_z(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%S.") + f"{dt.microsecond // 1000:03d}Z"


class TimeRebaser:
    """Rewrites sample post ids to fresh snowflakes, consistently (old id -> one new id)."""

    def __init__(self) -> None:
        self.mapping: dict[str, str] = {}
        self._last = 0
        self._used: set[int] = set()

    def _new_id(self, created_at: datetime, in_order: bool = True) -> str:
        if in_order:
            sf = max(datetime_to_snowflake(created_at), self._last + 1)  # strictly increasing: keeps order
            self._last = sf
        else:  # back-dated (e.g. a replayed outage catch-up): exact time, only avoid collisions
            sf = datetime_to_snowflake(created_at)
            while sf in self._used:
                sf += 1
        self._used.add(sf)
        return str(sf)

    def _map(self, value: Any) -> Any:
        old = _id_of(value)
        return self.mapping.get(old, value) if old is not None else value

    def rebase(self, payload: dict, created_at: datetime, *, in_order: bool = True) -> dict:
        """Return a copy of ``payload`` whose data.id is a snowflake for ``created_at``.

        Ids that refer to already-rebased samples (edit history, conversation, referenced posts and
        their includes) are rewritten with the same mapping so joins and edit detection keep working.
        New ids are strictly increasing in sample order unless ``in_order`` is false (back-dated
        samples keep their exact creation time).
        """
        out = copy.deepcopy(payload)
        data = out.get("data")
        if not isinstance(data, dict):
            return out
        old = _id_of(data.get("id"))
        if old is None:
            return out
        if old not in self.mapping:
            self.mapping[old] = self._new_id(created_at, in_order)
        new = self.mapping[old]
        data["id"] = new
        if "created_at" in data:
            data["created_at"] = _iso_z(snowflake_to_datetime(new))
        if isinstance(data.get("edit_history_tweet_ids"), list):
            data["edit_history_tweet_ids"] = [self._map(i) for i in data["edit_history_tweet_ids"]]
        if "conversation_id" in data:
            data["conversation_id"] = self._map(data["conversation_id"])
        for ref in data.get("referenced_tweets") or []:
            if isinstance(ref, dict) and "id" in ref:
                ref["id"] = self._map(ref["id"])
        includes = out.get("includes")
        if isinstance(includes, dict):
            for tw in includes.get("tweets") or []:
                if isinstance(tw, dict) and "id" in tw:
                    tw["id"] = self._map(tw["id"])
        return out


# --------------------------------------------------------------------------- dispatch wrapper


class ReplayDispatcher(AlertDispatcher):
    """Same channels as ``inner``, but every message is marked as a replay.

    The message is marked in place, so the AlertRecord the pipeline stores (and returns) carries the
    same ``[REPLAY]`` title as what was pushed - replay alerts stay recognizable even in a real DB.
    """

    def __init__(self, inner: AlertDispatcher) -> None:
        super().__init__(inner.settings, inner.push, sms=inner.sms, email=inner.email)

    @staticmethod
    def mark(msg: AlertMessage) -> AlertMessage:
        """Prefix title/SMS with ``[REPLAY]`` and add a note to the body (idempotent)."""
        if not msg.title.startswith(REPLAY_TAG):
            msg.sms_text = f"{REPLAY_TAG} {msg.sms_text or msg.title}"
            msg.title = f"{REPLAY_TAG} {msg.title}"
            msg.body = f"{msg.body}\n{REPLAY_NOTE}"
        return msg

    async def send(self, msg: AlertMessage, score: int | None = None) -> list[ChannelResult]:
        return await super().send(self.mark(msg), score)


# --------------------------------------------------------------------------- output


def _replay_config(cfg: AppConfig) -> AppConfig:
    """Sample ids are fake: match sample authors to watchlist accounts by handle, not by user id."""
    accounts = tuple(dataclasses.replace(a, user_id=None) for a in cfg.watchlist.accounts)
    return dataclasses.replace(cfg, watchlist=dataclasses.replace(cfg.watchlist, accounts=accounts))


def format_replay_line(index: int, res: HandleResult) -> str:
    """One line per sample: handle, flagged, score, decision."""
    prefix = f"[{index:02d}]"
    post = res.post
    if post is None:
        return f"{prefix} malformed payload skipped: {res.detail or 'unparseable'}"
    if post.author_username:
        handle = f"@{post.author_username}"
    elif res.account is not None:
        handle = f"@{res.account.handle}"
    else:
        handle = f"user {post.author_id}"
    if res.outcome == "duplicate":
        return f"{prefix} {handle:<17} duplicate id {post.id} - ignored (dedupe)"
    if res.outcome == "error":
        return f"{prefix} {handle:<17} pipeline error: {res.detail}"

    flagged = "flagged" if res.rules is not None and res.rules.flagged else "-"
    sc = res.score
    if sc is not None:
        score_txt = f"score {sc.impact:>2}/10 {sc.direction:<7}"
        if sc.is_joke:
            score_txt += " joke"
    else:
        score_txt = "not scored"
    tickers = ",".join(sc.tickers) if sc is not None and sc.tickers else ""
    if res.outcome == "alert" and res.alert is not None:
        rec = res.alert
        prio = res.decision.priority if res.decision is not None else ""
        state = "ALERT" if rec.sent_ok else "ALERT FAILED"
        if rec.late:
            state = f"LATE {state}"
        latency = f"{rec.total_latency_ms / 1000:.2f}s" if rec.total_latency_ms is not None else "n/a"
        verdict = f"{state} {prio} ({res.detail}) latency {latency}"
    elif res.outcome == "edit_skipped":
        verdict = f"no alert: {res.detail}"
    else:
        verdict = f"no alert ({res.detail})"
    parts = [prefix, f"{handle:<17}", f"{flagged:<7}", f"{score_txt:<24}", verdict]
    if tickers:
        parts.append(tickers)
    return " ".join(parts)


def _summary(results: list[HandleResult], scorer_name: str, channels: list[str]) -> list[str]:
    alerts = [r.alert for r in results if r.alert is not None]
    delivered = [a for a in alerts if a.sent_ok]
    late = [a for a in delivered if a.late]
    # like /health: late (catch-up) alerts say nothing about the live post -> phone latency
    latencies = [a.total_latency_ms for a in delivered if a.total_latency_ms is not None and not a.late]
    count = {o: sum(1 for r in results if r.outcome == o) for o in ("duplicate", "malformed", "error")}
    flagged = sum(1 for r in results if r.rules is not None and r.rules.flagged)
    scored = sum(1 for r in results if r.score is not None)
    processed = len(results) - count["duplicate"] - count["malformed"]
    line = (
        f"Replay summary: {len(results)} samples | {processed} processed | {count['duplicate']} duplicate | "
        f"{count['malformed']} malformed | {flagged} flagged | {scored} scored | "
        f"{len(alerts)} alerts ({len(delivered)} delivered)"
    )
    if late:
        line += f" | {len(late)} late"
    if count["error"]:
        line += f" | {count['error']} errors"
    lines = [line]
    if latencies:
        avg = sum(latencies) / len(latencies) / 1000
        lines.append(f"Latency post->phone: avg {avg:.2f}s, max {max(latencies) / 1000:.2f}s (target < 10s)")
    if late:
        lines.append(
            f"Late (catch-up) alerts: {len(late)} - sent low-key as 'Missed during outage', no SMS, "
            "not counted in the latency"
        )
    lines.append(f"Scorer: {scorer_name}. Channels: {', '.join(channels) or 'none'}. X API was not contacted.")
    return lines


def _sample_age(payload: dict, default: float) -> tuple[float, bool]:
    """(seconds between post creation and receipt, whether the sample asked for it via AGE_KEY)."""
    value = payload.get(AGE_KEY)
    if isinstance(value, (int, float)) and not isinstance(value, bool) and value >= 0:
        return float(value), True
    return default, False


# --------------------------------------------------------------------------- entry point


async def run_replay(
    cfg: AppConfig,
    path: str | Path,
    *,
    use_claude: bool = False,
    send: bool = False,
    db_path: str = ":memory:",
    rebase_times: bool = True,
    x_delay_seconds: float = 3.0,
    out: Callable[[str], Any] = print,
) -> list[AlertRecord]:
    """Run every sample in ``path`` through the full pipeline; returns the alerts it produced."""
    samples = load_samples(path)
    rcfg = _replay_config(cfg)

    scorer: Scorer
    if use_claude:
        scorer = ClaudeScorer(cfg.settings.scoring, cfg.secrets.anthropic_api_key)
        scorer_name = f"Claude ({cfg.settings.scoring.model})"
        if not cfg.secrets.anthropic_api_key:
            out("Note: ANTHROPIC_API_KEY is not set - Claude scoring will fall back to fast-rules scores.")
    else:
        scorer = OfflineScorer(cfg.settings.scoring)
        scorer_name = "offline fast rules (add --claude to score with Claude)"

    store = Store(db_path)
    session: aiohttp.ClientSession | None = None
    try:
        if send:
            session = new_session()
            base = build_dispatcher(rcfg, session)
            out(f"Sending replay alerts to your real channels: {', '.join(base.channel_names) or 'none'}")
        else:
            base = AlertDispatcher(cfg.settings.alerts, push=[ConsoleNotifier(out=out)])
        dispatcher = ReplayDispatcher(base)
        status = IngestStatus(mode="replay", state="replay")
        pipeline = Pipeline(rcfg, store, scorer, dispatcher, status)

        if is_quiet_hours(cfg.settings, now_utc()):
            qh = cfg.settings.alerts.quiet_hours
            sc = cfg.settings.scoring
            out(
                f"Note: quiet hours are active now ({qh.start}-{qh.end} {cfg.settings.alerts.timezone}); "
                f"only scores >= {sc.quiet_hours_threshold} alert."
            )
            if not use_claude and int(sc.fallback_max_impact) < int(sc.quiet_hours_threshold):
                out(
                    f"Note: offline scores are rules-only and capped at {sc.fallback_max_impact} "
                    f"(scoring.fallback_max_impact), so nothing alerts now except always-alert accounts with "
                    "the quiet-hours bypass; add --claude or replay outside quiet hours."
                )
        out(f"Replaying {len(samples)} sample(s) from {path} (synthetic fixtures; X is never contacted)")

        rebaser = TimeRebaser()
        results: list[HandleResult] = []
        for i, payload in enumerate(samples, 1):
            received = now_utc()
            if rebase_times:
                age, back_dated = _sample_age(payload, x_delay_seconds)
                payload = rebaser.rebase(payload, received - timedelta(seconds=age), in_order=not back_dated)
            res = await pipeline.process(IngestItem(payload=payload, received_at=received, source="replay"))
            results.append(res)
            out(format_replay_line(i, res))

        for line in _summary(results, scorer_name, dispatcher.channel_names):
            out(line)
        return [r.alert for r in results if r.alert is not None]
    finally:
        try:
            aclose = getattr(scorer, "aclose", None)
            if aclose is not None:
                await aclose()
        finally:
            if session is not None:
                await session.close()
            store.close()


__all__ = ["ReplayDispatcher", "TimeRebaser", "format_replay_line", "load_samples", "run_replay"]
