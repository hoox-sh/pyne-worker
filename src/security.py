# Copyright (c) 2026 HOOX · PYNE · jango-blockchained
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Shared security helpers for pyne-worker.

Covers:
- R2 path segment sanitization (symbol / timeframe) — path traversal defense
- Webhook URL validation — SSRF defense for alert delivery
"""

from __future__ import annotations

import ipaddress
import re
from urllib.parse import urlparse

# Trading symbols: bare (BTCUSDT) or exchange-prefixed (BINANCE:BTCUSDT).
# No path separators; length-capped for R2 key safety.
_SYMBOL_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,31}$")

_VALID_TIMEFRAMES = frozenset(
    {
        "1m",
        "3m",
        "5m",
        "15m",
        "30m",
        "1h",
        "2h",
        "4h",
        "6h",
        "8h",
        "12h",
        "1d",
        "3d",
        "1w",
        "1M",
    }
)

# Hostnames that must never be used as webhook destinations.
_BLOCKED_HOSTS = frozenset(
    {
        "localhost",
        "localhost.localdomain",
        "metadata",
        "metadata.google.internal",
        "metadata.goog",
        "kubernetes.default",
        "kubernetes.default.svc",
    }
)

_MAX_WEBHOOK_URL_LEN = 2048


def sanitize_symbol(symbol: str | None) -> str | None:
    """Normalize and validate a trading symbol for R2 keys / feed URLs.

    Returns uppercased symbol, or ``None`` if invalid / path-unsafe.
    """
    if not symbol or not isinstance(symbol, str):
        return None
    s = symbol.strip().upper()
    if not s or len(s) > 32:
        return None
    # Explicit path / traversal guards (regex also rejects most of these)
    if "/" in s or "\\" in s or ".." in s or "\x00" in s:
        return None
    if not _SYMBOL_RE.match(s):
        return None
    return s


def sanitize_timeframe(timeframe: str | None) -> str | None:
    """Return *timeframe* if it is a known Pine/Binance-style interval, else None."""
    if not timeframe or not isinstance(timeframe, str):
        return None
    tf = timeframe.strip()
    if tf not in _VALID_TIMEFRAMES:
        return None
    return tf


def is_blocked_ip(host: str) -> bool:
    """True if *host* is a literal IP in a non-public range (SSRF surface)."""
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return False
    return bool(
        ip.is_private
        or ip.is_loopback
        or ip.is_link_local
        or ip.is_reserved
        or ip.is_multicast
        or ip.is_unspecified
        or (getattr(ip, "is_site_local", False))
    )


def validate_webhook_url(url: str | None, *, allow_http: bool = False) -> str | None:
    """Validate a webhook destination against SSRF-ish abuse.

    Rules (fail-closed):
      - Non-empty string, max length
      - Scheme ``https`` (or ``http`` only when *allow_http* — tests only)
      - No credentials in netloc (``user:pass@host``)
      - Hostname required; not a blocked name / ``*.local`` / ``*.internal``
      - Literal private / loopback / link-local / metadata IPs rejected

    Returns the stripped URL on success, else ``None``.
    """
    if not url or not isinstance(url, str):
        return None
    u = url.strip()
    if not u or len(u) > _MAX_WEBHOOK_URL_LEN:
        return None
    if any(c in u for c in ("\r", "\n", "\x00")):
        return None

    try:
        parsed = urlparse(u)
    except Exception:
        return None

    scheme = (parsed.scheme or "").lower()
    if scheme == "https":
        pass
    elif scheme == "http" and allow_http:
        pass
    else:
        return None

    if parsed.username is not None or parsed.password is not None:
        return None

    host = (parsed.hostname or "").strip().lower()
    if not host:
        return None

    if host in _BLOCKED_HOSTS:
        return None
    if host.endswith(".local") or host.endswith(".internal") or host.endswith(".localhost"):
        return None
    if host.endswith(".localdomain"):
        return None

    # IPv6 literals arrive without brackets from urlparse.hostname
    if is_blocked_ip(host):
        return None

    # Reject numeric-only hosts that aren't valid public IPs (caught above)
    # and obvious metadata tricks like 169.254.169.254 already covered.

    return u


def safe_error_message(exc: BaseException, *, prefix: str = "error") -> str:
    """Build a client-facing error string without leaking secrets/paths.

    Strips common secret-looking substrings; keeps type + short detail.
    """
    et = type(exc).__name__
    detail = str(exc).strip() or et
    # Redact anything that looks like a bearer/api key fragment
    detail = re.sub(
        r"(?i)(api[_-]?key|authorization|bearer|secret|password)\s*[:=]\s*\S+",
        r"\1=[redacted]",
        detail,
    )
    # Cap length so huge stack-ish messages don't flood responses
    if len(detail) > 300:
        detail = detail[:297] + "..."
    if detail == et:
        return f"{prefix}: {et}"
    return f"{prefix}: {et}: {detail}"
