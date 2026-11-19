"""Unit tests for scoring response parsing and the fast-rules fallback (scoring/claude.py)."""

from __future__ import annotations

import json
from typing import Any

import pytest

from ceoscan.models import Account, RuleResult
from ceoscan.scoring.claude import (
    MAX_REASON_CHARS,
    ScoreParseError,
    fallback_score,
    parse_score_response,
)
from ceoscan.scoring.prompt import SCORE_JSON_SCHEMA

MY = ("TSLA", "NVDA", "MSTR")


def payload(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "tickers": ["TSLA"],
        "direction": "bullish",
        "impact_score": 8,
        "reason": "Tesla raised delivery guidance.",
        "is_joke_or_meme": False,
    }
    base.update(overrides)
    return base


def as_text(**overrides: Any) -> str:
    return json.dumps(payload(**overrides))


# --------------------------------------------------------------------------- happy paths


def test_valid_json() -> None:
    score = parse_score_response(as_text(), MY)
    assert score.tickers == ["TSLA"]
    assert score.direction == "bullish"
    assert score.impact == 8
    assert score.reason == "Tesla raised delivery guidance."
    assert score.is_joke is False
    assert score.source == "claude"
    assert score.error is None


@pytest.mark.parametrize(
    "text",
    [
        "```json\n" + as_text() + "\n```",
        "```\n" + as_text() + "\n```",
        "```JSON " + as_text() + "```",
        "Here is my answer:\n```json\n" + as_text() + "\n```\nHope that helps!",
    ],
)
def test_fenced_json(text: str) -> None:
    assert parse_score_response(text, MY).impact == 8


@pytest.mark.parametrize(
    "text",
    [
        "Sure! " + as_text() + " Let me know if you need more.",
        "Analysis {draft}: the post is material.\n" + as_text(),
        as_text() + "\n\nNote: scores are estimates.",
        '{"note": "not it"} and then ' + as_text(),
    ],
)
def test_prose_around_json(text: str) -> None:
    score = parse_score_response(text, MY)
    assert score.impact == 8 and score.tickers == ["TSLA"]


def test_pretty_printed_json_with_whitespace() -> None:
    assert parse_score_response("\n\n  " + json.dumps(payload(), indent=2) + "  \n", MY).direction == "bullish"


# --------------------------------------------------------------------------- impact


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (8, 8),
        (8.0, 8),
        ("8", 8),
        (" 7 ", 7),
        ("9/10", 9),
        (6.6, 7),
        ("4.4", 4),
        (0, 1),
        (-3, 1),
        (15, 10),
        ("11", 10),
        (10.9, 10),
    ],
)
def test_impact_coercion_and_clamping(value: Any, expected: int) -> None:
    score = parse_score_response(as_text(impact_score=value), MY)
    assert score.impact == expected
    assert isinstance(score.impact, int)


@pytest.mark.parametrize("value", [None, True, "high", "", [8], {"v": 8}])
def test_bad_impact_raises(value: Any) -> None:
    with pytest.raises(ScoreParseError):
        parse_score_response(as_text(impact_score=value), MY)


def test_nan_impact_raises() -> None:
    text = as_text().replace('"impact_score": 8', '"impact_score": NaN')
    with pytest.raises(ScoreParseError):
        parse_score_response(text, MY)


# --------------------------------------------------------------------------- direction


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("bullish", "bullish"),
        ("Bullish", "bullish"),
        (" BULL ", "bullish"),
        ("positive", "bullish"),
        ("bearish", "bearish"),
        ("bear", "bearish"),
        ("Negative", "bearish"),
        ("unclear", "unclear"),
        ("neutral", "unclear"),
        ("mixed", "unclear"),
        ("", "unclear"),
        (None, "unclear"),
        (5, "unclear"),
    ],
)
def test_direction_normalization(value: Any, expected: str) -> None:
    assert parse_score_response(as_text(direction=value), MY).direction == expected


# --------------------------------------------------------------------------- tickers


def test_ticker_cleanup_and_my_tickers_first() -> None:
    raw = ["$aapl", "XYZ", " tsla ", "NVDA", "$TSLA", "bad ticker!", "", 42, None, "BRK.B", "1ABC", "TOOLONGTICKER"]
    score = parse_score_response(as_text(tickers=raw), MY)
    # my tickers first (in the order the model gave them), then the rest; invalid dropped; deduped
    assert score.tickers == ["TSLA", "NVDA", "AAPL", "XYZ", "BRK.B"]


def test_tickers_without_my_tickers_keep_model_order() -> None:
    assert parse_score_response(as_text(tickers=["nvda", "TSLA"])).tickers == ["NVDA", "TSLA"]


def test_tickers_as_comma_string() -> None:
    assert parse_score_response(as_text(tickers="$AAPL, tsla nvda"), MY).tickers == ["TSLA", "NVDA", "AAPL"]


def test_empty_or_null_tickers() -> None:
    assert parse_score_response(as_text(tickers=[]), MY).tickers == []
    assert parse_score_response(as_text(tickers=None), MY).tickers == []


def test_tickers_wrong_type_raises() -> None:
    with pytest.raises(ScoreParseError):
        parse_score_response(as_text(tickers={"TSLA": 1}), MY)


# --------------------------------------------------------------------------- reason / joke


def test_reason_is_one_trimmed_line() -> None:
    score = parse_score_response(as_text(reason="  Big news.\n\nSecond   line\there.  "), MY)
    assert score.reason == "Big news. Second line here."


def test_long_reason_is_truncated() -> None:
    score = parse_score_response(as_text(reason="word " * 200), MY)
    assert len(score.reason) <= MAX_REASON_CHARS == 240
    assert score.reason.endswith("…")
    assert "\n" not in score.reason


@pytest.mark.parametrize(("value", "expected"), [(True, True), (False, False), ("true", True), ("no", False), (1, True), (None, False)])
def test_joke_flag(value: Any, expected: bool) -> None:
    assert parse_score_response(as_text(is_joke_or_meme=value), MY).is_joke is expected


# --------------------------------------------------------------------------- errors


@pytest.mark.parametrize("field", ["tickers", "direction", "impact_score", "reason", "is_joke_or_meme"])
def test_missing_required_field_raises(field: str) -> None:
    data = payload()
    del data[field]
    with pytest.raises(ScoreParseError, match=field):
        parse_score_response(json.dumps(data), MY)


@pytest.mark.parametrize(
    "text",
    ["", "   ", "I cannot help with that.", "[1, 2, 3]", '"just a string"', "{not json}", "```json\n{broken\n```"],
)
def test_no_json_object_raises(text: str) -> None:
    with pytest.raises(ScoreParseError):
        parse_score_response(text, MY)


def test_score_parse_error_is_value_error() -> None:
    assert issubclass(ScoreParseError, ValueError)


# --------------------------------------------------------------------------- schema


def test_schema_matches_parser_contract() -> None:
    assert SCORE_JSON_SCHEMA["type"] == "object"
    assert SCORE_JSON_SCHEMA["additionalProperties"] is False
    assert set(SCORE_JSON_SCHEMA["required"]) == set(SCORE_JSON_SCHEMA["properties"]) == {
        "tickers",
        "direction",
        "impact_score",
        "reason",
        "is_joke_or_meme",
    }
    props = SCORE_JSON_SCHEMA["properties"]
    assert props["direction"]["enum"] == ["bullish", "bearish", "unclear"]
    assert props["impact_score"]["type"] == "integer"
    assert props["tickers"] == {**props["tickers"], "type": "array", "items": {"type": "string"}}
    assert props["is_joke_or_meme"]["type"] == "boolean"
    # structured outputs reject numeric/string constraints: clamping happens in the parser
    flat = json.dumps(SCORE_JSON_SCHEMA)
    for unsupported in ("minimum", "maximum", "minLength", "maxLength", "maxItems"):
        assert unsupported not in flat


# --------------------------------------------------------------------------- fallback_score

ACCOUNT = Account(handle="elonmusk", tickers=("TSLA", "SPCX"))


def test_fallback_uses_rule_tickers_and_score() -> None:
    rules = RuleResult(
        flagged=True,
        reasons=["cashtag $NVDA", "keyword: guidance"],
        categories=["ticker", "guidance"],
        tickers=["NVDA"],
        rule_score=6,
    )
    score = fallback_score(rules, ACCOUNT, "timeout after 4s")
    assert score.tickers == ["NVDA"]
    assert score.direction == "unclear"
    assert score.impact == 6
    assert "cashtag $NVDA" in score.reason and "keyword: guidance" in score.reason
    assert score.is_joke is False
    assert score.source == "rules_fallback"
    assert score.error == "timeout after 4s"


def test_fallback_uses_account_tickers_when_rules_found_none() -> None:
    score = fallback_score(RuleResult(flagged=True, reasons=["keyword: launch"], rule_score=1), ACCOUNT, None)
    assert score.tickers == ["TSLA", "SPCX"]
    assert score.error is None


def test_fallback_minimum_impact_and_no_account() -> None:
    score = fallback_score(RuleResult(flagged=False), None, "Claude unavailable", source="offline")
    assert score.impact == 1
    assert score.tickers == []
    assert score.source == "offline"
    assert score.reason  # never empty
    assert len(score.reason) <= MAX_REASON_CHARS


def test_fallback_caps_impact_and_reason_length() -> None:
    rules = RuleResult(flagged=True, reasons=["number: " + "9" * 100] * 5, rule_score=42)
    score = fallback_score(rules, None, None)
    assert score.impact == 10
    assert len(score.reason) <= MAX_REASON_CHARS


def test_fallback_does_not_alias_rule_lists() -> None:
    rules = RuleResult(flagged=True, tickers=["TSLA"], rule_score=3)
    score = fallback_score(rules, None, None)
    score.tickers.append("X")
    assert rules.tickers == ["TSLA"]
