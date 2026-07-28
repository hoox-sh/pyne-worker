# Copyright (c) 2026 HOOX · PYNE · jango-blockchained
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Middleware for pyne-worker — auth, rate limiting, structured logging.

This module provides three utilities used by the request pipeline:

1. ``validate_api_key`` — constant-time API key comparison.
2. ``RateLimiter`` — sliding-window in-memory rate limiter.
3. ``LogHelper`` — structured JSON log builder with request IDs.
"""

# pyne-worker — Python Cloudflare Worker for Pine Script evaluation
# Copyright (C) 2024-2026  jango-blockchained
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU Affero General Public License for more details.
#
# You should have received a copy of the GNU Affero General Public License
# along with this program.  If not, see <https://www.gnu.org/licenses/>.

from __future__ import annotations

import hmac
import time
import uuid
from typing import Any


# ---------------------------------------------------------------------------
# Auth — constant-time API key validation
# ---------------------------------------------------------------------------


def validate_api_key(
    api_key: str | None,
    expected_key: str | None,
) -> bool:
    """Validate an API key using constant-time comparison.

    Args:
        api_key: The key provided by the client (``X-API-Key`` header).
        expected_key: The expected key from environment / secrets.

    Returns:
        ``True`` if the key is valid. If ``expected_key`` is ``None`` or empty
        (dev mode), all requests are allowed.
    """
    if not expected_key:
        return True  # dev mode — no key configured
    if not api_key:
        return False
    return hmac.compare_digest(api_key, expected_key)


# ---------------------------------------------------------------------------
# Rate Limiter — sliding-window in-memory
# ---------------------------------------------------------------------------


class RateLimiter:
    """Sliding-window in-memory rate limiter.

    State resets on cold start — acceptable for Workers where the lifetime
    of each isolate is measured in minutes under light traffic. For durable
    rate limiting across cold starts a KV-backed store should be added.

    Args:
        max_requests: Maximum requests allowed in the window.
        window_seconds: Width of the sliding window in seconds.
    """

    def __init__(self, max_requests: int = 100, window_seconds: int = 60) -> None:
        self._max_requests = max_requests
        self._window_seconds = window_seconds
        self._buckets: dict[str, list[float]] = {}

    def check(self, key: str) -> tuple[bool, dict[str, str]]:
        """Check whether a request should be allowed.

        Args:
            key: Client identifier (API key prefix or source IP).

        Returns:
            ``(allowed, headers)`` where ``headers`` contains
            ``X-RateLimit-*`` fields that can be merged into the response.
        """
        now = time.time()
        cutoff = now - self._window_seconds

        timestamps = self._buckets.get(key, [])
        timestamps = [t for t in timestamps if t > cutoff]

        allowed = len(timestamps) < self._max_requests
        remaining = max(0, self._max_requests - len(timestamps))
        reset_epoch = int(now) + self._window_seconds

        if allowed:
            timestamps.append(now)
            self._buckets[key] = timestamps

        return allowed, {
            "X-RateLimit-Limit": str(self._max_requests),
            "X-RateLimit-Remaining": str(remaining),
            "X-RateLimit-Reset": str(reset_epoch),
        }


# ---------------------------------------------------------------------------
# Structured logging helpers
# ---------------------------------------------------------------------------


class LogHelper:
    """Factory and helpers for structured JSON request logs."""

    @staticmethod
    def make_request_id() -> str:
        """Return a short unique request identifier."""
        return uuid.uuid4().hex[:12]

    @staticmethod
    def as_dict(
        request_id: str,
        method: str,
        path: str,
        status: int,
        duration_ms: float,
        **extra: Any,
    ) -> dict[str, Any]:
        """Build a structured log dict.

        Args:
            request_id: Unique per-request ID.
            method: HTTP method.
            path: Request path.
            status: HTTP status code.
            duration_ms: Wall-clock duration in milliseconds.
            **extra: Additional key/value pairs to include.

        Returns:
            Dict suitable for ``json.dumps``.
        """
        return {
            "request_id": request_id,
            "method": method,
            "path": path,
            "status": status,
            "duration_ms": round(duration_ms, 1),
            **extra,
        }
