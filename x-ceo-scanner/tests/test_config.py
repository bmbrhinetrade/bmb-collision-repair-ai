"""Tests for ceoscan.config: strict settings / watchlist validation and the .env loader."""

from __future__ import annotations

import builtins
import os
from pathlib import Path
from typing import Any

import pytest

from ceoscan.config import (
    ConfigError,
    Settings,
    fallback_settings,
    load_dotenv_if_present,
    load_settings,
    load_watchlist,
)

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(autouse=True)
def no_env_overrides(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("CEOSCAN_HEALTH_HOST", raising=False)
    monkeypatch.delenv("CEOSCAN_CONFIG_ERROR_ACTION", raising=False)


def settings_file(tmp_path: Path, text: str) -> Path:
    p = tmp_path / "settings.yaml"
    p.write_text(text, encoding="utf-8")
    return p


def watchlist_file(tmp_path: Path, text: str) -> Path:
    p = tmp_path / "watchlist.yaml"
    p.write_text(text, encoding="utf-8")
    return p


# --------------------------------------------------------------------------- shipped files


def test_shipped_config_files_load() -> None:
    s = load_settings(ROOT / "config" / "settings.yaml")
    assert s.ops.health_host == "127.0.0.1"  # SEC-9: local only unless the Docker image overrides it
    assert s.ops.config_error_action == "exit"
    assert s.scoring.total_budget_seconds == 4.0 and s.scoring.fallback_max_impact == 7
    assert s.x_api.recovery.max_pages == 5 and s.x_api.recovery.max_age_seconds == 3600
    assert s.alerts.max_post_age_seconds == 900 and s.alerts.email.timeout_seconds == 20.0
    wl = load_watchlist(ROOT / "config" / "watchlist.yaml")
    assert wl.accounts and all(len(t) > 1 for a in wl.accounts for t in a.tickers if t not in ("V", "F"))


def test_defaults_are_valid() -> None:
    assert load_settings(Path("/nonexistent/settings.yaml")) == Settings()


def test_env_overrides(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CEOSCAN_HEALTH_HOST", "0.0.0.0")
    monkeypatch.setenv("CEOSCAN_CONFIG_ERROR_ACTION", "wait")
    s = load_settings(settings_file(tmp_path, "ops:\n  health_host: 127.0.0.1\n"))
    assert s.ops.health_host == "0.0.0.0" and s.ops.config_error_action == "wait"
    monkeypatch.setenv("CEOSCAN_CONFIG_ERROR_ACTION", "sleep")
    with pytest.raises(ConfigError, match="CEOSCAN_CONFIG_ERROR_ACTION"):
        load_settings(settings_file(tmp_path, ""))
    assert fallback_settings().ops.config_error_action == "exit"  # never a broken value in the fallback
    monkeypatch.setenv("CEOSCAN_CONFIG_ERROR_ACTION", "wait")
    assert fallback_settings().ops.config_error_action == "wait"


# --------------------------------------------------------------------------- settings: SEC-3


@pytest.mark.parametrize(
    ("yaml_text", "needle"),
    [
        ('alerts:\n  quiet_hours:\n    start: "10pm"\n', 'alerts.quiet_hours.start must be a 24-hour time "HH:MM"'),
        ('alerts:\n  quiet_hours:\n    end: "22:00:00"\n', "alerts.quiet_hours.end must be a 24-hour time"),
        ('alerts:\n  quiet_hours:\n    start: "22.00"\n', "alerts.quiet_hours.start"),
        ('alerts:\n  quiet_hours:\n    start: "25:00"\n', "alerts.quiet_hours.start"),
        ("alerts:\n  quiet_hours:\n    start: 2200\n", "alerts.quiet_hours.start must be a text value"),
        ('ops:\n  daily_summary:\n    time: "6pm"\n', "ops.daily_summary.time must be a 24-hour time"),
        ("alerts:\n  timezone: America/LosAngeles\n", "alerts.timezone: unknown time zone 'America/LosAngeles'"),
        ("alerts:\n  timezone: Pacific\n", "alerts.timezone: unknown time zone"),
        ("alerts:\n  timezone: ../../etc/passwd\n", "alerts.timezone"),
        ('ops:\n  health_port: "8080"\n', "ops.health_port must be a whole number"),
        ("ops:\n  health_port: 70000\n", "ops.health_port must be 0-65535"),
        ("ops:\n  pipeline_workers: 0\n", "ops.pipeline_workers must be >= 1"),
        ("ops:\n  log_level: LOUD\n", "ops.log_level must be one of"),
        ("ops:\n  config_error_action: retry\n", "ops.config_error_action must be exit or wait"),
        ("scoring:\n  threshold: 11\n", "scoring.threshold must be 1-10"),
        ("scoring:\n  threshold: 7.5\n", "scoring.threshold must be a whole number"),
        ("scoring:\n  enabled: yes please\n", "scoring.enabled must be true or false"),
        ('scoring:\n  enabled: "false"\n', "scoring.enabled must be true or false"),
        ("scoring:\n  timeout_seconds: 0\n", "scoring.timeout_seconds must be > 0"),
        ("scoring:\n  total_budget_seconds: -1\n", "scoring.total_budget_seconds must be > 0"),
        ("scoring:\n  fallback_max_impact: 0\n", "scoring.fallback_max_impact must be 1-10"),
        (
            "scoring:\n  model: claude-opus-5-5\n  thinking: disabled\n",
            "scoring.thinking=disabled is rejected by claude-opus-5-5 at every effort",
        ),
        ("scoring:\n  model: claude-sonnet-5-5\n  thinking: disabled\n  effort: low\n", "rejected by claude-sonnet"),
        ("scoring:\n  model: claude-fable-5-2\n  thinking: disabled\n", "rejected by claude-fable-5-2"),
        ("scoring:\n  thinking: disabled\n  effort: xhigh\n", "scoring.thinking=disabled requires effort"),
        ("alerts:\n  send_timeout_seconds: fast\n", "alerts.send_timeout_seconds must be a number"),
        ("alerts:\n  push: telegram\n", "alerts.push must be ntfy, pushover, both or none"),
        ("alerts:\n  ntfy:\n    server: ntfy.sh\n", "alerts.ntfy.server must start with https://"),
        ("x_api:\n  recovery:\n    max_pages: 0\n", "x_api.recovery.max_pages must be >= 1"),
        ("x_api:\n  poll:\n    utilization: 2\n", "x_api.poll.utilization"),
        ("x_api:\n  mode: firehose\n", "x_api.mode must be auto, stream or poll"),
        ("x_api:\n  tweet_fields: created_at,author_id\n", "settings.x_api.tweet_fields must be a list"),
        ("alerts: ntfy\n", "settings.alerts must be a mapping"),
        (
            "alerts:\n  quiet_hour:\n    enabled: false\n",
            "unknown setting(s) at settings.alerts: quiet_hour (did you mean quiet_hours?)",
        ),
    ],
)
def test_invalid_settings_fail_at_load_time(tmp_path: Path, yaml_text: str, needle: str) -> None:
    with pytest.raises(ConfigError) as info:
        load_settings(settings_file(tmp_path, yaml_text))
    assert needle in str(info.value)


def test_several_problems_are_reported_together(tmp_path: Path) -> None:
    text = 'alerts:\n  timezone: Mars/Olympus\n  quiet_hours:\n    start: "10pm"\nops:\n  pipeline_workers: 0\n'
    with pytest.raises(ConfigError) as info:
        load_settings(settings_file(tmp_path, text))
    msg = str(info.value)
    assert msg.startswith("invalid settings:")
    assert "alerts.timezone" in msg and "alerts.quiet_hours.start" in msg and "ops.pipeline_workers" in msg


def test_valid_values_are_accepted_and_coerced(tmp_path: Path) -> None:
    text = (
        "alerts:\n  timezone: Europe/Berlin\n  quiet_hours:\n    start: '7:05'\n    end: '23:59'\n"
        "  send_timeout_seconds: 3\n"
        "ops:\n  health_port: 0\n  log_level: debug\n  daily_summary:\n    time: '00:00'\n"
        "scoring:\n  threshold: 8.0\n"
        "x_api:\n  pricing:\n    checked: 2026-11-19\n"
    )
    s = load_settings(settings_file(tmp_path, text))
    assert s.alerts.send_timeout_seconds == 3.0 and isinstance(s.alerts.send_timeout_seconds, float)
    assert s.scoring.threshold == 8 and isinstance(s.scoring.threshold, int)
    assert s.x_api.pricing.checked == "2026-11-19"  # an unquoted YAML date becomes text
    assert s.alerts.pushover.sound is None


@pytest.mark.parametrize("model", ["claude-haiku-5-5", "claude-opus-5", "claude-sonnet-5"])
def test_thinking_disabled_is_allowed_where_the_model_accepts_it(tmp_path: Path, model: str) -> None:
    s = load_settings(settings_file(tmp_path, f"scoring:\n  model: {model}\n  thinking: disabled\n  effort: high\n"))
    assert s.scoring.thinking == "disabled"


# --------------------------------------------------------------------------- watchlist: SEC-4


def test_scalar_tickers_are_rejected_not_split_into_letters(tmp_path: Path) -> None:
    p = watchlist_file(tmp_path, "accounts:\n  - handle: elonmusk\n    tickers: TSLA\n")
    with pytest.raises(ConfigError) as info:
        load_watchlist(p)
    msg = str(info.value)
    assert "accounts[0] (@elonmusk).tickers must be a list" in msg and "[TSLA]" in msg


@pytest.mark.parametrize(
    ("yaml_text", "needle"),
    [
        (
            "accounts:\n  - handle: elonmusk\n    tickers: [TSLA]\n    always-alert: true\n",
            "unknown key(s) at accounts[0] (@elonmusk): always-alert (did you mean always_alert?). Allowed keys:",
        ),
        (
            "accounts:\n  - handle: elonmusk\n    ticker: [TSLA]\n",
            "unknown key(s) at accounts[0] (@elonmusk): ticker (did you mean tickers?)",
        ),
        (
            "accounts:\n  - handle: elonmusk\n    include_reply: false\n",
            "include_reply (did you mean include_replies?)",
        ),
        ("accounts:\n  - elonmusk\n", "accounts[0] must be a mapping"),
        ("accounts:\n  handle: elonmusk\n", "accounts must be a list"),
        ("accounts:\n  - handle: elonmusk\n    always_alert: 'false'\n", "always_alert must be true or false"),
        ("accounts:\n  - handle: elonmusk\n    est_posts_per_day: lots\n", "est_posts_per_day must be a number"),
        ("accounts:\n  - kind: person\n", "invalid or missing handle"),
        ("accounts:\n  - handle: elonmusk\n    tickers: ['@nogroup']\n", "unknown ticker group '@nogroup'"),
        ("acounts:\n  - handle: elonmusk\n", "acounts (did you mean accounts?)"),
        ("my_tickers: APMD\n", "my_tickers must be a list"),
        ("ticker_groups:\n  big_tech: AAPL\n", "ticker_groups.big_tech must be a list"),
        ("ticker_groups: [AAPL]\n", "ticker_groups must be a mapping"),
        ("company_names:\n  TSLA: Tesla\n", "company_names.TSLA must be a list"),
        ("keyword_rules:\n  - name: big\n    tickers: TSLA\n", "keyword_rules[0].tickers must be a list"),
        ("keyword_rules:\n  - name: big\n    min_follower: 5\n", "min_follower (did you mean min_followers?)"),
        ("keyword_rules:\n  - name: big\n    min_followers: lots\n", "min_followers must be a whole number"),
        ("- handle: elonmusk\n", "the watchlist (top level) must be a mapping"),
    ],
)
def test_invalid_watchlists_fail_with_clear_errors(tmp_path: Path, yaml_text: str, needle: str) -> None:
    with pytest.raises(ConfigError) as info:
        load_watchlist(watchlist_file(tmp_path, yaml_text))
    assert needle in str(info.value)


def test_valid_watchlist_still_loads(tmp_path: Path) -> None:
    text = (
        "ticker_groups:\n  bitcoin: [MSTR, CLSK]\n"
        "my_tickers: [APMD]\n"
        "accounts:\n"
        "  - handle: '@saylor'\n    tickers: ['@bitcoin', $COIN]\n    user_id: 244647486\n    notes: ~\n"
        "  - handle: elonmusk\n    tickers: []\n    always_alert: true\n"
        "keyword_rules:\n  - name: big\n    tickers: all\n    extra_operators: ~\n"
        "company_names:\n  TSLA: [Tesla, 'Model Y']\n"
    )
    wl = load_watchlist(watchlist_file(tmp_path, text))
    saylor, elon = wl.accounts
    assert saylor.handle == "saylor" and saylor.tickers == ("MSTR", "CLSK", "COIN") and saylor.user_id == "244647486"
    assert elon.always_alert is True and elon.tickers == ()
    assert wl.keyword_rules[0].tickers == ("APMD", "MSTR", "CLSK", "COIN")
    assert wl.keyword_rules[0].extra_operators == ""
    assert wl.company_names == {"TSLA": ("Tesla", "Model Y")}
    assert load_watchlist(watchlist_file(tmp_path, "")).accounts == ()


# --------------------------------------------------------------------------- .env: SEC-2


def test_unreadable_dotenv_is_a_warning_not_a_crash(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    env = tmp_path / ".env"
    env.write_text("CEOSCAN_TEST_SECRET=abcdef123456\n", encoding="utf-8")
    real_open = builtins.open

    def fake_open(file: Any, *a: Any, **k: Any) -> Any:
        if str(file) == str(env):
            raise PermissionError(13, "Permission denied", str(file))
        return real_open(file, *a, **k)

    monkeypatch.setattr(builtins, "open", fake_open)
    monkeypatch.delenv("CEOSCAN_TEST_SECRET", raising=False)
    load_dotenv_if_present(env)  # must not raise
    err = capsys.readouterr().err
    assert err.strip() == f"warning: cannot read {env} (Permission denied); using the existing environment"
    assert "abcdef123456" not in err
    assert "CEOSCAN_TEST_SECRET" not in os.environ


def test_dotenv_is_a_directory_or_binary(tmp_path: Path) -> None:
    d = tmp_path / "dir.env"
    d.mkdir()
    load_dotenv_if_present(d)  # never raises
    b = tmp_path / "bin.env"
    b.write_bytes(b"\xff\xfe\x00garbage")
    load_dotenv_if_present(b)


def test_dotenv_loads_without_overriding(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    env = tmp_path / ".env"
    env.write_text("CEOSCAN_A=from-file\nCEOSCAN_B=from-file\n", encoding="utf-8")
    monkeypatch.setenv("CEOSCAN_A", "from-env")
    monkeypatch.delenv("CEOSCAN_B", raising=False)
    load_dotenv_if_present(env)
    assert os.environ["CEOSCAN_A"] == "from-env" and os.environ["CEOSCAN_B"] == "from-file"
    monkeypatch.delenv("CEOSCAN_B", raising=False)
    load_dotenv_if_present(tmp_path / "missing.env")  # silently ignored
