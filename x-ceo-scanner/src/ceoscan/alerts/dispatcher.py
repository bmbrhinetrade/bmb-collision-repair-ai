"""Fan an AlertMessage out to the configured channels.

* Push (ntfy and/or Pushover) gets every message.
* SMS: post alerts with score >= ``alerts.sms.min_score``, or any urgent message; never summaries.
* Email: score >= ``alerts.email.min_score``, system alerts, and the daily summary when
  ``alerts.email.send_daily_summary``.

All selected channels run concurrently, each bounded by ``alerts.send_timeout_seconds``, so one slow or
failing channel never delays or blocks another. ``send`` never raises.
"""

from __future__ import annotations

import asyncio
import dataclasses
import logging
import time

import aiohttp

from ..config import AlertSettings, AppConfig
from ..models import AlertMessage, ChannelResult
from .notifiers import EmailNotifier, Notifier, NtfyNotifier, PushoverNotifier, TwilioSmsNotifier

logger = logging.getLogger(__name__)


def _name(notifier: Notifier) -> str:
    return str(getattr(notifier, "name", type(notifier).__name__))


class AlertDispatcher:
    def __init__(
        self,
        settings: AlertSettings,
        push: list[Notifier],
        sms: Notifier | None = None,
        email: Notifier | None = None,
    ) -> None:
        self.settings = settings
        self.push = list(push)
        self.sms = sms
        self.email = email

    @property
    def channel_names(self) -> list[str]:
        names = [_name(n) for n in self.push]
        if self.sms is not None:
            names.append(_name(self.sms))
        if self.email is not None:
            names.append(_name(self.email))
        return names

    def wants_sms(self, msg: AlertMessage, score: int | None) -> bool:
        if self.sms is None or msg.kind == "summary":
            return False
        return (score is not None and score >= self.settings.sms.min_score) or msg.priority == "urgent"

    def wants_email(self, msg: AlertMessage, score: int | None) -> bool:
        if self.email is None:
            return False
        return (
            (score is not None and score >= self.settings.email.min_score)
            or (msg.kind == "summary" and self.settings.email.send_daily_summary)
            or msg.kind == "system"
        )

    def select(self, msg: AlertMessage, score: int | None = None) -> list[Notifier]:
        """The channels this message goes to, in result order (push..., sms, email)."""
        chosen = list(self.push)
        if self.wants_sms(msg, score):
            assert self.sms is not None
            chosen.append(self.sms)
        if self.wants_email(msg, score):
            assert self.email is not None
            chosen.append(self.email)
        return chosen

    async def _send_one(
        self, notifier: Notifier, msg: AlertMessage, score: int | None, t0: float
    ) -> ChannelResult:
        name = _name(notifier)
        timeout = float(self.settings.send_timeout_seconds)
        try:
            if isinstance(notifier, PushoverNotifier):
                coro = notifier.send(msg, score=score)  # honors pushover.high_priority_min_score
            else:
                coro = notifier.send(msg)
            result = await asyncio.wait_for(coro, timeout=timeout)
        except (asyncio.TimeoutError, TimeoutError):
            result = ChannelResult(name, False, 0, error=f"timeout after {timeout:g}s")
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # a broken notifier must never take down the others
            logger.warning("alert channel %s raised %s", name, type(exc).__name__)
            result = ChannelResult(name, False, 0, error=f"unexpected {type(exc).__name__}")
        if not isinstance(result, ChannelResult):
            result = ChannelResult(name, False, 0, error="notifier returned no result")
        # elapsed is measured from dispatch start so callers can compute "dispatch -> first success".
        result = dataclasses.replace(result, elapsed_ms=int((time.monotonic() - t0) * 1000))
        if not result.ok:
            logger.warning(
                "alert channel %s failed after %d ms: %s", result.channel, result.elapsed_ms, result.error
            )
        return result

    async def send(self, msg: AlertMessage, score: int | None = None) -> list[ChannelResult]:
        """Send to every selected channel concurrently. Returns one result per channel; never raises."""
        channels = self.select(msg, score)
        if not channels:
            logger.warning("no alert channel configured; alert not delivered: %s", msg.title)
            return []
        t0 = time.monotonic()
        results = await asyncio.gather(*(self._send_one(n, msg, score, t0) for n in channels))
        return list(results)


# --------------------------------------------------------------------------- factory


def _missing(pairs: list[tuple[str, str | None]]) -> list[str]:
    return [env for env, value in pairs if not value]


def build_dispatcher(cfg: AppConfig, session: aiohttp.ClientSession) -> AlertDispatcher:
    """Build the dispatcher from settings + secrets.

    A configured channel whose secrets are missing is skipped with a warning naming the missing
    environment variables (names only, never values).
    """
    a = cfg.settings.alerts
    sec = cfg.secrets
    timeout = float(a.send_timeout_seconds)

    push: list[Notifier] = []
    if a.push in ("ntfy", "both"):
        missing = _missing([("NTFY_TOPIC", sec.ntfy_topic)])
        if missing:
            logger.warning("alerts.push=%s but %s is not set - ntfy push disabled", a.push, ", ".join(missing))
        else:
            assert sec.ntfy_topic is not None
            push.append(NtfyNotifier(session, a.ntfy.server, sec.ntfy_topic, sec.ntfy_token, timeout))
    if a.push in ("pushover", "both"):
        missing = _missing(
            [("PUSHOVER_APP_TOKEN", sec.pushover_app_token), ("PUSHOVER_USER_KEY", sec.pushover_user_key)]
        )
        if missing:
            logger.warning(
                "alerts.push=%s but %s not set - Pushover push disabled", a.push, ", ".join(missing)
            )
        else:
            assert sec.pushover_app_token is not None and sec.pushover_user_key is not None
            push.append(
                PushoverNotifier(session, sec.pushover_app_token, sec.pushover_user_key, a.pushover, timeout)
            )

    sms: Notifier | None = None
    if a.sms.enabled:
        missing = _missing(
            [
                ("TWILIO_ACCOUNT_SID", sec.twilio_account_sid),
                ("TWILIO_AUTH_TOKEN", sec.twilio_auth_token),
                ("TWILIO_FROM", sec.twilio_from),
                ("ALERT_SMS_TO", sec.sms_to),
            ]
        )
        if missing:
            logger.warning("alerts.sms.enabled but %s not set - SMS disabled", ", ".join(missing))
        else:
            assert sec.twilio_account_sid and sec.twilio_auth_token and sec.twilio_from and sec.sms_to
            sms = TwilioSmsNotifier(
                session, sec.twilio_account_sid, sec.twilio_auth_token, sec.twilio_from, sec.sms_to, timeout
            )

    email: Notifier | None = None
    if a.email.enabled:
        missing = _missing(
            [("SMTP_HOST", sec.smtp_host), ("ALERT_EMAIL_FROM", sec.email_from), ("ALERT_EMAIL_TO", sec.email_to)]
        )
        if missing:
            logger.warning("alerts.email.enabled but %s not set - email disabled", ", ".join(missing))
        else:
            if bool(sec.smtp_username) != bool(sec.smtp_password):
                logger.warning(
                    "only one of SMTP_USERNAME / SMTP_PASSWORD is set - sending email without SMTP login"
                )
            email = EmailNotifier(sec, a.email, timeout)

    dispatcher = AlertDispatcher(a, push, sms=sms, email=email)
    if dispatcher.channel_names:
        logger.info("alert channels: %s", ", ".join(dispatcher.channel_names))
    else:
        logger.warning("no alert channels configured (alerts.push=%s); alerts will only be logged", a.push)
    return dispatcher
