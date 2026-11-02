# Copyright (c) 2026 HOOX · PYNE · jango-blockchained
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Shared security helpers for pyne-worker.

Covers:
- R2 path segment sanitization (symbol / timeframe) — path traversal defense
- Exchange interval → Pine ``timeframe.period`` mapping for Runtime
- Webhook URL validation — SSRF defense for alert delivery
- Client-facing engine error sanitization (Pro API 0.6.30 parity)
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

# Exchange / Binance-style interval → Pine ``timeframe.period``. Passing the
# raw worker string ``"1m"`` through Runtime would upper-case to ``"1M"``
# (monthly). Chart ``timeframe.*`` needs the Pine form.
_EXCHANGE_TO_PINE_PERIOD: dict[str, str] = {
    "1m": "1",
    "3m": "3",
    "5m": "5",
    "15m": "15",
    "30m": "30",
    "1h": "60",
    "2h": "120",
    "4h": "240",
    "6h": "360",
    "8h": "480",
    "12h": "720",
    "1d": "D",
    "3d": "3D",
    "1w": "W",
    "1M": "M",
}

_INTERNAL_PATH_RE = re.compile(r'(?:File ")?[\\/](?:[\w.-]+[\\/])*[\w.-]+\.py(?:", line \d+)?')
_MAX_ERROR_MSG_LEN = 500
_TRACE_PREFIXES = (
    "Traceback ",
    "File ",
    "During handling of",
    "The above exception",
)


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


def to_pine_period(timeframe: str | None) -> str | None:
    """Map an exchange interval (``1m``, ``4h``, ``1d``) to Pine ``timeframe.period``.

    Runtime constructor ``timeframe=`` expects ``"15"``, ``"60"``, ``"D"``,
    ``"W"``, ``"M"``, ``"30S"``. ``None`` keeps the engine's daily default.

    Already-Pine forms (numeric minutes, ``D``/``W``/``M``, ``30S``) pass
    through. Unknown strings return ``None`` so a garbage interval cannot
    become monthly via ``"1m".upper() == "1M"``.
    """
    if not timeframe or not isinstance(timeframe, str):
        return None
    tf = timeframe.strip()
    if not tf:
        return None
    mapped = _EXCHANGE_TO_PINE_PERIOD.get(tf)
    if mapped is not None:
        return mapped
    if tf.isdigit():
        return tf
    u = tf.upper()
    aliases = {"1D": "D", "1W": "W", "1M": "M"}
    if u in aliases:
        return aliases[u]
    if u in {"D", "W", "M", "3D"}:
        return u
    if len(tf) >= 2 and tf[:-1].isdigit() and tf[-1] in "SsHhDdWw":
        return tf
    if len(tf) >= 3 and tf[:-2].isdigit() and tf[-2:].upper() == "MO":
        return tf
    return None


def _parse_literal_ip(host: str) -> ipaddress.IPv4Address | ipaddress.IPv6Address | None:
    """Parse a hostname that is itself an IP (dotted, IPv6, or decimal IPv4).

    Decimal (``2130706433``) is the spelled-out form of ``127.0.0.1`` that
    ``urlparse``/``ip_address`` do not normalize — mirror the Pro API's
    hardening so it cannot smuggle a private target past the literal check.
    """
    if not host:
        return None
    try:
        return ipaddress.ip_address(host)
    except ValueError:
        pass
    if not host.isdigit():
        return None
    try:
        return ipaddress.IPv4Address(int(host))
    except (ValueError, OverflowError):
        return None


def _unwrap_ip(
    ip: ipaddress.IPv4Address | ipaddress.IPv6Address,
) -> ipaddress.IPv4Address | ipaddress.IPv6Address:
    """Unwrap IPv4-mapped / 6to4 so ``::ffff:127.0.0.1`` reads as loopback."""
    if isinstance(ip, ipaddress.IPv6Address):
        mapped = ip.ipv4_mapped
        if mapped is not None:
            return mapped
        sixtofour = ip.sixtofour
        if sixtofour is not None:
            return sixtofour
    return ip


def is_blocked_ip(host: str) -> bool:
    """True if *host* is a literal IP in a non-public range (SSRF surface)."""
    ip = _parse_literal_ip(host)
    if ip is None:
        return False
    ip = _unwrap_ip(ip)
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
      - Literal private / loopback / link-local / metadata IPs rejected,
        including IPv4-mapped/6to4 IPv6 and decimal IPv4 spellings

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


def sanitize_error_for_response(msg: str) -> str:
    """One-line client error: drop traceback frames and source paths.

    Mirrors Flask Pro API ``_sanitize_error_for_response`` (pynescript 0.6.30)
    so ``/run`` failures never leak isolate paths. Also redacts secret-looking
    ``key=value`` fragments.
    """
    last = ""
    for line in str(msg).splitlines():
        s = line.strip()
        if not s:
            continue
        if s.startswith(_TRACE_PREFIXES) or _INTERNAL_PATH_RE.search(s):
            continue
        last = s
    last = re.sub(
        r"(?i)(api[_-]?key|authorization|bearer|secret|password)\s*[:=]\s*\S+",
        r"\1=[redacted]",
        last,
    )
    if len(last) > _MAX_ERROR_MSG_LEN:
        last = last[:_MAX_ERROR_MSG_LEN] + "..."
    return last or "An error occurred"
