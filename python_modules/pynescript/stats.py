# Copyright (C) 2024-2026 jango_blockchained
#
# This file is part of pynescript.
#
# pynescript is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# pynescript is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU Affero General Public License for more details.
#
# You should have received a copy of the GNU Affero General Public License
# along with pynescript.  If not, see <https://www.gnu.org/licenses/>.
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""In-process runtime statistics for the Pro API (``GET /stats``).

Deliberately dependency-free and process-local. It complements — never
replaces — the client-visible per-run profile (``total_ms`` / ``parse_ms`` /
``eval_ms`` in the ``/run`` envelope) and the compile/session counters already
embedded in ``GET /health``.

Three metric kinds
------------------
``count``
    Monotonic totals. ``count("run.total")``, optionally by dimension:
    ``count("run.by_mode", 1, mode="interpret")``.
``latency``
    Duration samples in **seconds**, summarised on read as
    count / avg / min / max / p50 / p95 (ms). A bounded reservoir of the most
    recent :data:`SAMPLE_LIMIT` samples backs the percentiles.
``gauge``
    Last-value snapshot (``sessions.active``, ``compile_cache.entries``).

Everything is guarded by a single :class:`threading.Lock`; hot-path calls are
microseconds and never raise. Names are dotted strings (``run.total``,
``delta.bars_appended``) so the snapshot groups naturally.
"""

from __future__ import annotations

import threading
import time

from collections import deque
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any


__all__ = [
    "SAMPLE_LIMIT",
    "count",
    "dec_gauge",
    "gauge",
    "inc_gauge",
    "latency",
    "max_gauge",
    "reset",
    "set_gauge",
    "snapshot",
    "started_at",
    "timer",
    "uptime_seconds",
]


#: Recent samples retained per latency metric (for p50/p95).
SAMPLE_LIMIT = 512

#: Sentinel meaning "no observation yet" (min/max must not start at 0).
_NEG_INF = float("-inf")
_POS_INF = float("inf")


class _Counter:
    """Monotonic total plus optional per-dimension breakdown."""

    __slots__ = ("dims", "total")

    def __init__(self) -> None:
        self.total: float = 0
        self.dims: dict[str, float] = {}

    def add(self, value: float, dim: str | None) -> None:
        self.total += value
        if dim is not None:
            self.dims[dim] = self.dims.get(dim, 0.0) + value


class _Latency:
    """Duration accumulator with a bounded sample reservoir."""

    __slots__ = ("count", "max", "min", "samples", "total")

    def __init__(self) -> None:
        self.count: int = 0
        self.total: float = 0.0
        self.min: float = _POS_INF
        self.max: float = _NEG_INF
        self.samples: deque[float] = deque(maxlen=SAMPLE_LIMIT)

    def add(self, seconds: float) -> None:
        self.count += 1
        self.total += seconds
        self.min = min(self.min, seconds)
        self.max = max(self.max, seconds)
        self.samples.append(seconds)

    def summary(self) -> dict[str, Any]:
        if self.count == 0:
            return {"count": 0}
        ordered = sorted(self.samples)
        return {
            "count": self.count,
            "total_ms": round(self.total * 1000.0, 3),
            "avg_ms": round((self.total / self.count) * 1000.0, 3),
            "min_ms": round(self.min * 1000.0, 3),
            "max_ms": round(self.max * 1000.0, 3),
            "p50_ms": round(_percentile(ordered, 0.50) * 1000.0, 3),
            "p95_ms": round(_percentile(ordered, 0.95) * 1000.0, 3),
        }


def _percentile(ordered: list[float], q: float) -> float:
    """Nearest-rank percentile over a pre-sorted list (0.0 when empty).

    Args:
        ordered: Pre-sorted float samples.
        q: Quantile in [0.0, 1.0].

    Returns:
        float: The percentile value.
    """
    if not ordered:
        return 0.0
    # Nearest-rank index bounded to valid range.
    idx = round(q * (len(ordered) - 1))
    return ordered[max(0, min(idx, len(ordered) - 1))]


_lock = threading.Lock()
_counters: dict[str, _Counter] = {}
_latencies: dict[str, _Latency] = {}
_gauges: dict[str, float] = {}
_STARTED_AT = time.time()


def started_at() -> float:
    """Unix timestamp of process/module import (uptime anchor).

    Returns:
        float: Unix timestamp when the module was imported.
    """
    return _STARTED_AT


def uptime_seconds() -> float:
    """Seconds since this module was imported into the process.

    Returns:
        float: Elapsed seconds since import.
    """
    return time.time() - _STARTED_AT


@contextmanager
def _swallow() -> Iterator[None]:
    """Run a metric update, dropping any error.

    Stats are observability, never a correctness dependency: a broken metric
    update must not be able to fail a ``/run``, a delta append or a prewarm.
    """
    try:
        yield
    except Exception:  # see docstring: observability must never raise
        pass


def count(name: str, value: float = 1, dim: str | None = None) -> None:
    """Add ``value`` to counter ``name``, optionally into dimension ``dim``.

    ``dim`` is a short label (``mode="interpret"``, ``endpoint="POST /run"``).
    Dimension values are unbounded in theory but always come from a small
    closed set (route names, mode names), so the breakdown stays small.
    """
    with _swallow(), _lock:
        bucket = _counters.get(name)
        if bucket is None:
            bucket = _counters[name] = _Counter()
        bucket.add(value, dim)


def latency(name: str, seconds: float) -> None:
    """Record a duration (in seconds) for latency metric ``name``."""
    with _swallow(), _lock:
        bucket = _latencies.get(name)
        if bucket is None:
            bucket = _latencies[name] = _Latency()
        bucket.add(seconds)


def set_gauge(name: str, value: float) -> None:
    """Record the current value of gauge ``name`` (last write wins)."""
    with _swallow(), _lock:
        _gauges[name] = value


#: Alias kept for call sites that read better as a setter.
gauge = set_gauge


def inc_gauge(name: str, value: float = 1) -> None:
    """Add ``value`` to gauge ``name`` (concurrent-request style counters)."""
    with _swallow(), _lock:
        _gauges[name] = _gauges.get(name, 0) + value


def dec_gauge(name: str, value: float = 1) -> None:
    """Subtract ``value`` from gauge ``name``, clamped at 0."""
    with _swallow(), _lock:
        _gauges[name] = max(0.0, _gauges.get(name, 0) - value)


def max_gauge(name: str, value: float) -> None:
    """Raise gauge ``name`` to ``value`` if currently lower (never lowers it).

    Used for high-water marks (e.g. largest single delta append) so the number
    stays meaningful without keeping a separate histogram.
    """
    with _swallow(), _lock:
        cur = _gauges.get(name)
        if cur is None or value > cur:
            _gauges[name] = value


@contextmanager
def timer(name: str) -> Iterator[None]:
    """Context manager recording wall-clock seconds into latency ``name``.

    ``with timer("compile.total"): ...`` — safe to nest and cheap enough for
    the request path.
    """
    t0 = time.perf_counter()
    try:
        yield
    finally:
        latency(name, time.perf_counter() - t0)


def snapshot() -> dict[str, Any]:
    """Return a JSON-serializable copy of all metrics collected so far."""
    with _lock:
        counters = {name: {"total": _num(b.total), **_dim_dict(b)} for name, b in sorted(_counters.items())}
        latencies = {name: b.summary() for name, b in sorted(_latencies.items())}
        gauges = {name: _num(v) for name, v in sorted(_gauges.items())}
    return {
        "started_at": round(_STARTED_AT, 3),
        "uptime_seconds": round(uptime_seconds(), 3),
        "counters": counters,
        "latencies": latencies,
        "gauges": gauges,
    }


def reset() -> None:
    """Drop every recorded metric (test helper; not exposed over HTTP)."""
    with _lock:
        _counters.clear()
        _latencies.clear()
        _gauges.clear()


def _dim_dict(bucket: _Counter) -> dict[str, Any]:
    """Build dimension breakdown from a counter bucket."""
    if not bucket.dims:
        return {}
    return {"by": {k: _num(v) for k, v in sorted(bucket.dims.items())}}


def _num(value: float) -> float | int:
    """Return an ``int`` when the float is integral so JSON stays readable.

    Args:
        value: Numeric value to normalize.

    Returns:
        int if integral, else rounded float.
    """
    if float(value).is_integer():
        return int(value)
    return round(float(value), 3)
