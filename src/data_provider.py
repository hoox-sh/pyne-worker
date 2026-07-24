"""OHLCV data provider — reads historical bar data from R2 or inline.

File format (matching pine-worker):
  ``data/{SYMBOL}/{TIMEFRAME}/{YYYY}.jsonl.gz``

Each line is a JSON object with ``open``, ``high``, ``low``, ``close``,
``time`` (ms epoch), and optionally ``volume``.

Usage:
  >>> from data_provider import fetch_ohlcv_from_r2
  >>> bars = await fetch_ohlcv_from_r2(bucket, "BTCUSDT", "1d")
"""

from __future__ import annotations

import gzip
import json
from typing import Any


def _ohlcv_key(symbol: str, timeframe: str, year: int) -> str:
    """Return the R2 object key for a symbol/timeframe/year combo."""
    return f"data/{symbol.upper()}/{timeframe}/{year}.jsonl.gz"


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
        if obj is None:
            continue
        seen_years.add(year)

        raw: bytes | None = await obj.arrayBuffer()  # workers-py API
        if raw is None:
            continue

        try:
            decompressed = gzip.decompress(raw)
        except Exception:
            continue  # skip corrupt files

        for line in decompressed.decode("utf-8").splitlines():
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
        if obj is not None:
            return True
    return False
