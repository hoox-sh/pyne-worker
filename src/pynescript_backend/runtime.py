# Copyright (c) 2026 HOOX · PYNE · jango-blockchained
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Edge Runtime thin wrap over package :mod:`pynescript.runtime` (H1).

**Source of truth:** :mod:`pynescript.runtime.host` (installable package).
This module no longer duplicates the bar loop. Worker-only deltas:

- strict OHLCV bar validation (``error_kind=data``) — Pro soft-coerces
- re-exports host compile caches under this module for existing tests

``timeout_seconds`` / ``timed_out`` live on the package Runtime so Pro API
and edge share one circuit breaker.
"""

from __future__ import annotations

import sys
from typing import Any

from pynescript.runtime import host as _host
from pynescript.runtime.host import (  # noqa: F401 — public re-exports
    ERROR_KIND_COMPILE,
    ERROR_KIND_DATA,
    ERROR_KIND_MODE,
    ERROR_KIND_ORDER,
    ERROR_KIND_PARSE,
    ERROR_KIND_RUNTIME,
    Barstate,
    Chart,
    Chartinfo,
    LazyCalendarContext,
    Runtime as _PackageRuntime,
    Syminfo,
    Timeframe,
    _error_payload,
)

# Same dict objects as SoT so tests clearing caches here affect the host.
_HOST_COMPILE_CACHE = _host._HOST_COMPILE_CACHE
_HOST_COMPILE_CACHE_MAX = _host._HOST_COMPILE_CACHE_MAX
_HOST_COMPILE_FAIL_CACHE = _host._HOST_COMPILE_FAIL_CACHE
_HOST_COMPILE_FAIL_CACHE_MAX = _host._HOST_COMPILE_FAIL_CACHE_MAX

# Worker / HTTP hosts require full OHLCV keys (SoT Pro soft-coerces missing fields).
_REQUIRED_BAR_FIELDS = frozenset({"open", "high", "low", "close", "time"})


class _RuntimeModule(type(sys.modules[__name__])):  # type: ignore[misc]
    """Route ``_HAS_COMPILER`` get/set to package host (tests patch this name)."""

    def __getattribute__(self, name: str) -> Any:
        if name == "_HAS_COMPILER":
            return _host._HAS_COMPILER
        return super().__getattribute__(name)

    def __setattr__(self, name: str, value: Any) -> None:
        if name == "_HAS_COMPILER":
            _host._HAS_COMPILER = value
            return
        super().__setattr__(name, value)


sys.modules[__name__].__class__ = _RuntimeModule  # type: ignore[misc]


def _validate_bars(ohlcv_data: list[dict]) -> str | None:
    """Validate OHLCV bar data for the edge worker contract.

    Returns ``None`` if valid, or an error message string if invalid.
    """
    if not isinstance(ohlcv_data, list):
        return "OHLCV data must be a list"
    for i, bar in enumerate(ohlcv_data):
        if not isinstance(bar, dict):
            return f"Bar at index {i} is not a dict"
        missing = _REQUIRED_BAR_FIELDS - set(bar)
        if missing:
            return f"Bar at index {i} missing fields: {', '.join(sorted(missing))}"
    return None


class Runtime(_PackageRuntime):
    """Package Runtime with fail-closed OHLCV validation for edge hosts."""

    def run(
        self,
        source_code: str,
        ohlcv_data: list[dict],
        data_feed=None,
        data_provider=None,
        mode: str | None = None,
        inputs: dict | None = None,
        profiler: bool = False,
        timeout_seconds: float | None = None,
        **kwargs: Any,
    ):
        # Fail closed on malformed bars (handler also validates; this covers
        # scheduler/scripts/corpus callers that skip the HTTP path).
        bar_err = _validate_bars(ohlcv_data)
        if bar_err:
            return _error_payload(bar_err, kind=ERROR_KIND_DATA)
        return super().run(
            source_code,
            ohlcv_data,
            data_feed=data_feed,
            data_provider=data_provider,
            mode=mode,
            inputs=inputs,
            profiler=profiler,
            timeout_seconds=timeout_seconds,
            **kwargs,
        )


__all__ = [
    "ERROR_KIND_COMPILE",
    "ERROR_KIND_DATA",
    "ERROR_KIND_MODE",
    "ERROR_KIND_ORDER",
    "ERROR_KIND_PARSE",
    "ERROR_KIND_RUNTIME",
    "Barstate",
    "Chart",
    "Chartinfo",
    "LazyCalendarContext",
    "Runtime",
    "Syminfo",
    "Timeframe",
    "_HOST_COMPILE_CACHE",
    "_HOST_COMPILE_FAIL_CACHE",
    "_validate_bars",
    "_error_payload",
]
