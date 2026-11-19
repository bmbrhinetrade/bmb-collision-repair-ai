"""Shared aiohttp session factory.

`trust_env=True` makes aiohttp honor HTTPS_PROXY / NO_PROXY (and ~/.netrc), so the
service works on servers that reach the internet through an egress proxy. Without a
proxy configured it behaves exactly like a plain session.
"""

from __future__ import annotations

from typing import Any

import aiohttp


def new_session(**kwargs: Any) -> aiohttp.ClientSession:
    kwargs.setdefault("trust_env", True)
    return aiohttp.ClientSession(**kwargs)
