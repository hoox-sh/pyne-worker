# Copyright (c) 2026 HOOX · PYNE · jango-blockchained
# SPDX-License-Identifier: AGPL-3.0-or-later

"""CORS for AXIS / HOOX browser clients calling this Worker.

Mirrors AXIS Worker ``pickOrigin`` (product hosts + project-scoped Pages
previews + localhost) and Flask Pro API allow-headers (``X-API-Key``).
``GET /health`` is a free CORS path so the AXIS Settings probe works from
any https Origin. ``POST /run`` echoes only allowlisted Origins.
"""

from __future__ import annotations

import re

# Vite :3000, axis_pwa :8081, Playwright ephemeral ports. Not ``0.0.0.0``.
_LOCAL_DEV_ORIGIN_RE = re.compile(
    r"^https?://(?:localhost|127\.0\.0\.1)(?::\d+)?$",
    re.IGNORECASE,
)

# Same product set as AXIS worker pickOrigin — not open ``*.pages.dev``,
# and not the retired ``pynescript.ai`` host.
_PRODUCT_ORIGIN_RE = re.compile(
    r"^https://(?:"
    r"(?:[\w-]+\.)*(?:hoox\.sh|pynescript\.online)|"
    r"(?:[\w-]+\.)*(?:axis|pynescript-axis)\.pages\.dev"
    r")$",
    re.IGNORECASE,
)

_FREE_CORS_PATHS = frozenset({"/", "/health"})

_ALLOW_METHODS = "GET, POST, PUT, DELETE, OPTIONS, HEAD"
_ALLOW_HEADERS = "Content-Type, Authorization, X-API-Key, X-Api-Key, Accept"
_MAX_AGE = "86400"
_MAX_ORIGIN_LEN = 256


def _clean_origin(origin: str | None) -> str | None:
    if not origin or not isinstance(origin, str):
        return None
    origin = origin.strip()
    if not origin or origin == "null":
        return None
    if len(origin) > _MAX_ORIGIN_LEN or "\r" in origin or "\n" in origin:
        return None
    if not origin.startswith(("http://", "https://")):
        return None
    return origin


def _extra_list(extra_allowed: str | None) -> list[str]:
    if not extra_allowed or extra_allowed.strip() in {"", "*"}:
        return []
    return [p.strip() for p in extra_allowed.split(",") if p.strip()]


def origin_allowed(origin: str | None, extra_allowed: str | None = None) -> bool:
    """True when *origin* may be echoed on authenticated routes (``/run``, …)."""
    origin = _clean_origin(origin)
    if origin is None:
        return False
    if extra_allowed and extra_allowed.strip() in {"*", "any"}:
        return True
    if _LOCAL_DEV_ORIGIN_RE.fullmatch(origin):
        return True
    if _PRODUCT_ORIGIN_RE.fullmatch(origin):
        return True
    return origin in _extra_list(extra_allowed)


def path_is_free_cors(path: str) -> bool:
    """``GET /health`` (AXIS Settings probe) reflects any http(s) Origin."""
    p = (path or "/").split("?", 1)[0]
    if p != "/" and p.endswith("/"):
        p = p.rstrip("/")
    return p in _FREE_CORS_PATHS


def cors_headers_for(
    origin: str | None,
    path: str,
    extra_allowed: str | None = None,
) -> dict[str, str]:
    """``Access-Control-*`` headers for *origin*, or ``Vary: Origin`` only."""
    origin = _clean_origin(origin)
    if origin is None:
        return {}
    if path_is_free_cors(path) or origin_allowed(origin, extra_allowed):
        return {
            "Access-Control-Allow-Origin": origin,
            "Access-Control-Allow-Methods": _ALLOW_METHODS,
            "Access-Control-Allow-Headers": _ALLOW_HEADERS,
            "Access-Control-Max-Age": _MAX_AGE,
            "Vary": "Origin",
        }
    return {"Vary": "Origin"}


def attach_cors(
    headers: dict[str, str],
    origin: str | None,
    path: str,
    extra_allowed: str | None = None,
) -> dict[str, str]:
    """Merge CORS onto an existing response header map."""
    extra = cors_headers_for(origin, path, extra_allowed=extra_allowed)
    if not extra:
        return headers
    out = dict(headers)
    if "Vary" in out and extra.get("Vary"):
        seen: set[str] = set()
        parts: list[str] = []
        for item in f"{out['Vary']}, {extra['Vary']}".split(","):
            p = item.strip()
            key = p.lower()
            if p and key not in seen:
                seen.add(key)
                parts.append(p)
        extra = dict(extra)
        extra["Vary"] = ", ".join(parts)
    out.update(extra)
    return out
