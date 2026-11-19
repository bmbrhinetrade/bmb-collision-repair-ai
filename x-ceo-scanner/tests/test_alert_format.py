"""format_post_alert / format_system_alert content."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from ceoscan.alerts.format import format_post_alert, format_system_alert, snippet
from ceoscan.config import AlertSettings
from ceoscan.models import Account, Decision, Post, ReferencedPost, Score

ELON = Account(handle="elonmusk", tickers=("TSLA", "SPCX"), user_id="44196397")
VIP = Account(handle="saylor", tickers=("MSTR",), user_id="244647486", always_alert=True)
CREATED = datetime(2026, 11, 20, 4, 41, 7, 123000, tzinfo=UTC)  # = Nov 19, 8:41:07 PM PST


def make_post(text: str = "Tesla will start Robotaxi service in 10 new cities next month", **kw) -> Post:
    defaults = dict(
        id="1991234567890123456",
        text=text,
        author_id="44196397",
        created_at=CREATED,
        received_at=CREATED,
        source="stream",
        author_username="elonmusk",
    )
    defaults.update(kw)
    return Post(**defaults)  # type: ignore[arg-type]


def make_score(direction: str = "bullish", impact: int = 8, tickers=("TSLA", "SPCX"), **kw) -> Score:
    defaults = dict(
        tickers=list(tickers),
        direction=direction,
        impact=impact,
        reason="Robotaxi expansion to 10 cities is a material revenue catalyst.",
        is_joke=False,
        source="claude",
    )
    defaults.update(kw)
    return Score(**defaults)  # type: ignore[arg-type]


DEC = Decision(should_alert=True, reason="score 8 >= 7", quiet_hours=False, priority="high")


def lines(body: str) -> list[str]:
    return body.split("\n")


# --------------------------------------------------------------------------- title / header


def test_title_single_ticker_bullish() -> None:
    msg = format_post_alert(make_post(), ELON, make_score(tickers=["TSLA"]), DEC, AlertSettings())
    assert msg.title == "🟢 TSLA +8/10 @elonmusk"


@pytest.mark.parametrize(
    ("direction", "emoji", "sign"), [("bullish", "🟢", "+"), ("bearish", "🔴", "-"), ("unclear", "⚪", "")]
)
def test_emoji_by_direction(direction: str, emoji: str, sign: str) -> None:
    msg = format_post_alert(make_post(), ELON, make_score(direction=direction), DEC, AlertSettings())
    assert msg.title == f"{emoji} TSLA,SPCX {sign}8/10 @elonmusk"
    assert lines(msg.body)[0] == f"@elonmusk · TSLA, SPCX · {direction.upper()} · 8/10"
    assert direction in msg.tags


def test_body_layout_and_fields() -> None:
    post = make_post()
    msg = format_post_alert(post, ELON, make_score(), DEC, AlertSettings())
    body = lines(msg.body)
    assert body[0] == "@elonmusk · TSLA, SPCX · BULLISH · 8/10"
    assert body[1] == "Why: Robotaxi expansion to 10 cities is a material revenue catalyst."
    assert body[2] == f'"{post.text}"'  # short post: no ellipsis
    assert body[3] == "Posted Nov 19, 8:41:07 PM PT"
    assert body[-1] == "https://x.com/elonmusk/status/1991234567890123456"
    assert msg.url == post.url
    assert msg.kind == "post"
    assert msg.priority == "high"
    assert msg.tags[:2] == ["TSLA", "SPCX"]


def test_priority_comes_from_decision() -> None:
    dec = Decision(True, "score 9 >= 7", priority="urgent")
    assert format_post_alert(make_post(), ELON, make_score(impact=9), dec, AlertSettings()).priority == "urgent"


def test_posted_time_in_pacific_daylight_time() -> None:
    summer = datetime(2026, 7, 4, 16, 5, 9, tzinfo=UTC)  # 9:05:09 AM PDT
    msg = format_post_alert(make_post(created_at=summer), ELON, make_score(), DEC, AlertSettings())
    assert "Posted Jul 4, 9:05:09 AM PT" in lines(msg.body)


def test_reason_is_one_line() -> None:
    sc = make_score(reason="Line one\nline   two\r\n")
    msg = format_post_alert(make_post(), ELON, sc, DEC, AlertSettings())
    assert "Why: Line one line two" in lines(msg.body)


def test_handle_fallbacks() -> None:
    no_user = make_post(author_username=None)
    assert format_post_alert(no_user, ELON, make_score(), DEC, AlertSettings()).title.endswith("@elonmusk")
    anon = format_post_alert(no_user, None, make_score(), DEC, AlertSettings())
    assert anon.title.endswith("user 44196397")
    assert anon.url == "https://x.com/i/web/status/1991234567890123456"


def test_no_tickers_omitted_cleanly() -> None:
    msg = format_post_alert(make_post(), ELON, make_score(tickers=[]), DEC, AlertSettings())
    assert msg.title == "🟢 +8/10 @elonmusk"
    assert lines(msg.body)[0] == "@elonmusk · BULLISH · 8/10"


def test_many_tickers_title_compact() -> None:
    sc = make_score(tickers=["NVDA", "AMD", "SMCI", "MU", "CEG"])
    msg = format_post_alert(make_post(), ELON, sc, DEC, AlertSettings())
    assert msg.title.startswith("🟢 NVDA,AMD,SMCI+2 +8/10")
    assert "NVDA, AMD, SMCI, MU, CEG" in lines(msg.body)[0]


# --------------------------------------------------------------------------- snippet


def test_snippet_exactly_200_chars_plus_ellipsis() -> None:
    text = "x" * 500
    msg = format_post_alert(make_post(text=text), ELON, make_score(), DEC, AlertSettings())
    snip_line = lines(msg.body)[2]
    assert snip_line == '"' + "x" * 200 + '…"'
    inner = snip_line[1:-1]
    assert len(inner.rstrip("…")) == 200


def test_snippet_exactly_200_not_truncated() -> None:
    text = "y" * 200
    msg = format_post_alert(make_post(text=text), ELON, make_score(), DEC, AlertSettings())
    assert lines(msg.body)[2] == f'"{text}"'


def test_snippet_respects_setting_and_collapses_whitespace() -> None:
    s = AlertSettings(snippet_chars=10)
    msg = format_post_alert(make_post(text="Hello\n\nworld   this is long"), ELON, make_score(), DEC, s)
    assert lines(msg.body)[2] == '"Hello worl…"'


def test_snippet_helper() -> None:
    assert snippet("a &amp; b", 200) == "a & b"  # X escapes &, <, > in post text
    assert snippet("word " * 100, 200).endswith("…")
    assert len(snippet("word " * 100, 200)) <= 201
    assert snippet("anything", 0) == ""


def test_snippet_zero_drops_line() -> None:
    msg = format_post_alert(make_post(), ELON, make_score(), DEC, AlertSettings(snippet_chars=0))
    assert not any(line.startswith('"') for line in lines(msg.body))


# --------------------------------------------------------------------------- tag lines


def test_tag_lines() -> None:
    post = make_post(
        referenced=[
            ReferencedPost(type="replied_to", id="1", author_username="SawyerMerritt"),
            ReferencedPost(type="quoted", id="2", author_username="Tesla"),
        ],
        edit_history_ids=["1991234567890000000", "1991234567890123456"],
    )
    dec = Decision(True, "always-alert account @saylor", quiet_hours=True, priority="normal")
    sc = make_score(is_joke=True, source="rules_fallback")
    msg = format_post_alert(post, VIP, sc, dec, AlertSettings())
    body = msg.body
    assert "😜 Joke/meme" in body
    assert "✏️ Edited post" in body
    assert "↩️ Reply to @SawyerMerritt" in body
    assert "💬 Quotes @Tesla" in body
    assert "🌙 Quiet hours" in body
    assert "📌 Always-alert account" in body
    assert "Rules-only score (Claude unavailable)" in body
    assert lines(body)[-1] == post.url  # url stays last
    assert "joke" in msg.tags and "quiet-hours" in msg.tags


def test_no_tag_lines_for_plain_post() -> None:
    msg = format_post_alert(make_post(), ELON, make_score(), DEC, AlertSettings())
    assert len(lines(msg.body)) == 5


# --------------------------------------------------------------------------- sms


def test_sms_text_contents() -> None:
    post = make_post()
    msg = format_post_alert(post, ELON, make_score(), DEC, AlertSettings())
    assert msg.sms_text is not None
    assert msg.sms_text.startswith("@elonmusk TSLA,SPCX BULLISH 8/10: Robotaxi expansion")
    assert msg.sms_text.endswith(post.url)
    assert len(msg.sms_text) <= 300


def test_sms_text_long_reason_keeps_url() -> None:
    post = make_post()
    sc = make_score(reason="very long reason " * 40)
    msg = format_post_alert(post, ELON, sc, DEC, AlertSettings())
    assert msg.sms_text is not None
    assert len(msg.sms_text) <= 300
    assert msg.sms_text.endswith(post.url)
    assert "..." in msg.sms_text


# --------------------------------------------------------------------------- system


def test_format_system_alert() -> None:
    msg = format_system_alert("X stream down", "Disconnected for 2m 05s; reconnecting.")
    assert msg.kind == "system"
    assert msg.priority == "high"
    assert msg.title == "X stream down"
    assert msg.body == "Disconnected for 2m 05s; reconnecting."
    assert msg.url is None
    assert msg.sms_text is not None and "X stream down" in msg.sms_text and len(msg.sms_text) <= 300


def test_format_system_alert_priority() -> None:
    assert format_system_alert("ceoscan started", "mode stream", priority="normal").priority == "normal"
    long = format_system_alert("t", "b" * 1000)
    assert long.sms_text is not None and len(long.sms_text) <= 300


# --------------------------------------------------------------------------- rules-only label


@pytest.mark.parametrize(
    ("source", "why"),
    [("rules_fallback", "Claude unavailable"), ("offline", "Claude scoring not configured"),
     ("skipped", "not sent to Claude")],
)
def test_rules_only_scores_are_labelled(source: str, why: str) -> None:
    sc = make_score(direction="unclear", impact=7, tickers=["TSLA"], source=source, reason="Fast rules: q3")
    msg = format_post_alert(make_post(), ELON, sc, DEC, AlertSettings())
    assert msg.title == "⚪ TSLA 7/10 @elonmusk (rules-only)"
    assert f"ℹ️ Rules-only score ({why}) - keyword heuristics, not an AI judgment" in lines(msg.body)
    assert msg.sms_text is not None and msg.sms_text.startswith("@elonmusk TSLA UNCLEAR 7/10 (rules-only): Fast rules")
    assert "rules-only" in msg.tags


def test_claude_score_has_no_rules_only_label() -> None:
    msg = format_post_alert(make_post(), ELON, make_score(), DEC, AlertSettings())
    assert "rules-only" not in msg.title and "Rules-only" not in msg.body
    assert "rules-only" not in (msg.sms_text or "") and "rules-only" not in msg.tags


# --------------------------------------------------------------------------- late posts


def test_late_post_title_sms_and_body() -> None:
    post = make_post(received_at=CREATED + timedelta(hours=2, minutes=5))
    dec = Decision(True, "score 8 >= 7 (late: posted 2h 5m ago)", priority="low")
    msg = format_post_alert(post, ELON, make_score(tickers=["TSLA"]), dec, AlertSettings())
    assert msg.title == "Missed during outage - posted 2h 5m ago: 🟢 TSLA +8/10 @elonmusk"
    assert msg.priority == "low"
    assert msg.sms_text is not None
    assert msg.sms_text.startswith("[Missed during outage - posted 2h 5m ago] @elonmusk TSLA BULLISH 8/10: ")
    assert msg.sms_text.endswith(post.url) and len(msg.sms_text) <= 300
    body = lines(msg.body)
    assert "Posted Nov 19, 8:41:07 PM PT" in body
    assert "⏪ Late: received Nov 19, 10:46:07 PM PT, 2h 5m after it was posted (catch-up)" in body
    assert body[-1] == post.url
    assert "late" in msg.tags


def test_post_just_within_max_age_is_not_marked_late() -> None:
    post = make_post(received_at=CREATED + timedelta(seconds=900))
    msg = format_post_alert(post, ELON, make_score(), DEC, AlertSettings())
    assert not msg.title.startswith("Missed") and "late" not in msg.tags
    assert format_post_alert(post, ELON, make_score(), DEC, AlertSettings(max_post_age_seconds=60)).title.startswith(
        "Missed during outage - posted 15m ago: ")


def test_late_rules_only_edited_title_has_every_marker() -> None:
    post = make_post(received_at=CREATED + timedelta(days=2), edit_history_ids=["1", "1991234567890123456"])
    sc = make_score(direction="bearish", impact=7, tickers=["TSLA"], source="rules_fallback")
    msg = format_post_alert(post, ELON, sc, DEC, AlertSettings(), edit_note="was BULLISH, now BEARISH")
    assert msg.title == "Missed during outage - posted 2d ago: 🔴 TSLA -7/10 @elonmusk (edited, rules-only)"
    assert (msg.sms_text or "").startswith(
        "[Missed during outage - posted 2d ago] @elonmusk TSLA BEARISH 7/10 (edited, rules-only): ")
    assert len(msg.sms_text or "") <= 300


# --------------------------------------------------------------------------- edits


def test_edit_note_replaces_generic_edited_line() -> None:
    post = make_post(edit_history_ids=["1991234567890000000", "1991234567890123456"])
    msg = format_post_alert(post, ELON, make_score(direction="bearish"), DEC, AlertSettings(),
                            edit_note="was BULLISH, now BEARISH")
    body = lines(msg.body)
    assert "✏️ Edited: was BULLISH, now BEARISH" in body
    assert "✏️ Edited post" not in body
    assert msg.title.endswith("@elonmusk (edited)") and "edited" in msg.tags
    assert "(edited)" in (msg.sms_text or "")


def test_plain_edit_without_note_keeps_generic_line_and_title() -> None:
    post = make_post(edit_history_ids=["1991234567890000000", "1991234567890123456"])
    msg = format_post_alert(post, ELON, make_score(tickers=["TSLA"]), DEC, AlertSettings())
    assert "✏️ Edited post" in lines(msg.body)
    assert msg.title == "🟢 TSLA +8/10 @elonmusk"
