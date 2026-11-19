"""Logging with secret redaction.

Every configured secret value, and any `Bearer <token>` / `Basic <creds>` / `sk-ant-...` pattern, is
replaced with `***` in everything the handler writes: the message, exception text and tracebacks
(``logger.exception`` / ``exc_info=``) and stack info. Redaction happens in the formatter, on the
fully formatted line, because tracebacks are only rendered at format time (after the filters ran).
Code should still never log headers or secrets on purpose - this is a safety net.
"""

from __future__ import annotations

import logging
import re
import sys
from collections.abc import Iterable

_AUTH_RE = re.compile(r"(?i)\b(bearer|basic)\s+[A-Za-z0-9._~+/=%-]{8,}")
_KEYISH_RE = re.compile(r"\bsk-ant-[A-Za-z0-9_-]{8,}")

LOG_FORMAT = "%(asctime)s %(levelname)s %(name)s: %(message)s"


class RedactingFilter(logging.Filter):
    """Redacts the message of each record (``record.msg`` / ``args``) before any handler formats it."""

    def __init__(self, secrets: Iterable[str] = ()) -> None:
        super().__init__()
        self.secrets = sorted({s for s in secrets if s and len(s) >= 6}, key=len, reverse=True)

    def redact(self, text: str) -> str:
        for s in self.secrets:
            if s in text:
                text = text.replace(s, "***")
        text = _AUTH_RE.sub(lambda m: f"{m.group(1)} ***", text)
        text = _KEYISH_RE.sub("sk-ant-***", text)
        return text

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            msg = record.getMessage()
        except Exception:  # pragma: no cover - malformed log call
            return True
        redacted = self.redact(msg)
        if redacted != msg:
            record.msg = redacted
            record.args = None
        return True


class RedactingFormatter(logging.Formatter):
    """Formats normally, then redacts the whole result (message, exception text, traceback, stack)."""

    def __init__(self, redactor: RedactingFilter, fmt: str | None = LOG_FORMAT, datefmt: str | None = None) -> None:
        super().__init__(fmt, datefmt)
        self.redactor = redactor

    def format(self, record: logging.LogRecord) -> str:
        cached = record.exc_text
        try:
            text = super().format(record)
        finally:
            # Formatter.format caches the rendered (unredacted) traceback on the shared record;
            # do not leave it there for other handlers.
            record.exc_text = cached
        return self.redactor.redact(text)


def setup_logging(level: str = "INFO", secrets: Iterable[str] = ()) -> RedactingFilter:
    root = logging.getLogger()
    root.setLevel(level.upper())
    for h in list(root.handlers):
        root.removeHandler(h)
    handler = logging.StreamHandler(sys.stdout)
    flt = RedactingFilter(secrets)
    handler.setFormatter(RedactingFormatter(flt))
    handler.addFilter(flt)
    root.addHandler(handler)
    # Third-party libraries can be chatty (and some log request details at DEBUG).
    for noisy in ("httpx", "httpx2", "httpcore", "anthropic", "aiohttp.access"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    return flt
