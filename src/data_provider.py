# Copyright (c) 2026 HOOX · PYNE · jango-blockchained
# SPDX-License-Identifier: AGPL-3.0-or-later

"""OHLCV data provider — reads historical bar data from R2 or inline.

File format (JSONL, uncompressed):
  ``data/{SYMBOL}/{TIMEFRAME}/{YYYY}.jsonl``

Each line is a JSON object with ``open``, ``high``, ``low``, ``close``,
``time`` (ms epoch), and optionally ``volume``.

Usage:
  >>> from data_provider import fetch_ohlcv_from_r2
  >>> bars = await fetch_ohlcv_from_r2(bucket, "BTCUSDT", "1d")
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

import json
from typing import Any


# ---------------------------------------------------------------------------
# R2 helpers
# ---------------------------------------------------------------------------


def _is_r2_hit(obj: Any) -> bool:
    """Check if an R2 get/head result is a real object (not None/JsNull).

    In workers-py, R2 bucket operations return ``JsNull`` (JavaScript null)
    instead of Python ``None`` when a key doesn't exist.
    """
    if obj is None:
        return False
    # JsNull is falsy in workers-py
    if not obj:
        return False
    # A real R2 object must have arrayBuffer
    if not hasattr(obj, "arrayBuffer"):
        return False
    return True


def _ohlcv_key(symbol: str, timeframe: str, year: int) -> str:
    """Return the R2 object key for a symbol/timeframe/year combo."""
    return f"data/{symbol.upper()}/{timeframe}/{year}.jsonl"


async def fetch_ohlcv_from_r2(
    bucket: Any,
    symbol: str,
    timeframe: str,
    from_time: int = 0,
    to_time: int = 2**63 - 1,
) -> list[dict[str, Any]]:
    """Fetch OHLCV bars from an R2 bucket.

    Args:
        bucket: R2 bucket binding (``self.env.OHLCV_DATA``).
        symbol: Trading pair, e.g. ``"BTCUSDT"``.
        timeframe: Binance-style interval, e.g. ``"1d"``, ``"1h"``.
        from_time: Earliest timestamp (ms epoch), default 0.
        to_time: Latest timestamp (ms epoch), default far future.

    Returns:
        List of ``{open, high, low, close, time, volume?}`` dicts, sorted
        ascending by time. Empty list if no data found.
    """
    bars: list[dict[str, Any]] = []
    seen_years: set[int] = set()

    # Scan plausible years (current year back 10 years)
    import datetime

    now = datetime.datetime.now(datetime.timezone.utc)
    for year in range(now.year, now.year - 10, -1):
        key = _ohlcv_key(symbol, timeframe, year)
        obj = await bucket.get(key)
        if not _is_r2_hit(obj):
            continue
        seen_years.add(year)

        raw: str | None = await obj.text()  # workers-py API
        if raw is None:
            continue

        for line in raw.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                bar = json.loads(line)
            except json.JSONDecodeError:
                continue
            t = bar.get("time", 0)
            if from_time <= t <= to_time:
                bars.append(bar)

    if not seen_years:
        return []

    bars.sort(key=lambda b: b.get("time", 0))
    return bars


async def ingest_ohlcv_to_r2(
    bucket: Any,
    symbol: str,
    timeframe: str,
    bars: list[dict[str, Any]],
) -> int:
    """Ingest OHLCV bars into the R2 bucket.

    Bars are appended to the appropriate year file (gzipped JSONL).
    If a year file already exists, new bars are prepended and the full
    set is re-sorted by time before writing.

    Args:
        bucket: R2 bucket binding.
        symbol: Trading pair, e.g. ``"BTCUSDT"``.
        timeframe: Binance-style interval, e.g. ``"1d"``.
        bars: List of bar dicts to ingest.

    Returns:
        Number of bars ingested.
    """
    if not bars:
        return 0

    import datetime
    import gzip
    import io

    # Group bars by year
    now = datetime.datetime.now(datetime.timezone.utc)
    by_year: dict[int, list[dict[str, Any]]] = {}
    for bar in bars:
        t = bar.get("time", 0)
        if t == 0:
            # Default to current year if no time
            year = now.year
        else:
            year = datetime.datetime.fromtimestamp(t / 1000, tz=datetime.timezone.utc).year
        by_year.setdefault(year, []).append(bar)

    total = 0
    for year, year_bars in by_year.items():
        key = _ohlcv_key(symbol, timeframe, year)
        existing_bars: list[dict[str, Any]] = []

        # Read existing data
        obj = await bucket.get(key)
        if _is_r2_hit(obj):
            raw: str | None = await obj.text()
            if raw is not None:
                for line in raw.splitlines():
                    line = line.strip()
                    if line:
                        try:
                            existing_bars.append(json.loads(line))
                        except json.JSONDecodeError:
                            continue

        # Merge and deduplicate by time
        seen_times: set[int] = {b.get("time", 0) for b in existing_bars}
        for bar in year_bars:
            t = bar.get("time", 0)
            if t not in seen_times:
                existing_bars.append(bar)
                seen_times.add(t)

        existing_bars.sort(key=lambda b: b.get("time", 0))

        # Write back as JSONL (uncompressed — workers-py FFI doesn't support
        # passing Python bytes to R2 put; str is accepted directly).
        lines = "\n".join(json.dumps(b, separators=(",", ":")) for b in existing_bars)
        await bucket.put(key, lines)
        total += len(year_bars)

    return total


async def has_ohlcv_data(
    bucket: Any,
    symbol: str,
    timeframe: str,
) -> bool:
    """Check if OHLCV data exists for a symbol/timeframe.

    Args:
        bucket: R2 bucket binding.
        symbol: Trading pair.
        timeframe: Binance-style interval.

    Returns:
        ``True`` if at least one year file exists.
    """
    import datetime

    now = datetime.datetime.now(datetime.timezone.utc)
    for year in range(now.year, now.year - 3, -1):
        key = _ohlcv_key(symbol, timeframe, year)
        obj = await bucket.get(key)
        if _is_r2_hit(obj):
            return True
    return False
