"""Logging with secret redaction.

Every handler gets a filter that replaces any configured secret value, and any
`Bearer <token>` / `Basic <creds>` pattern, with `***`. Code should still never
log headers or secrets on purpose - this is a safety net.
"""

from __future__ import annotations

import logging
import re
import sys
from typing import Iterable

_AUTH_RE = re.compile(r"(?i)\b(bearer|basic)\s+[A-Za-z0-9._~+/=%-]{8,}")
_KEYISH_RE = re.compile(r"\bsk-ant-[A-Za-z0-9_-]{8,}")


class RedactingFilter(logging.Filter):
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
        if record.exc_info and record.exc_text:
            record.exc_text = self.redact(record.exc_text)
        return True


def setup_logging(level: str = "INFO", secrets: Iterable[str] = ()) -> RedactingFilter:
    root = logging.getLogger()
    root.setLevel(level.upper())
    for h in list(root.handlers):
        root.removeHandler(h)
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    flt = RedactingFilter(secrets)
    handler.addFilter(flt)
    root.addHandler(handler)
    # Third-party libraries can be chatty (and some log request details at DEBUG).
    for noisy in ("httpx", "httpx2", "httpcore", "anthropic", "aiohttp.access"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    return flt
