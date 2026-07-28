#!/usr/bin/env python3
# Copyright (c) 2026 HOOX · PYNE · jango-blockchained
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Fetch OHLCV data from Binance and write to gzipped JSONL format for R2 ingestion.

Usage:
  # Fetch BTCUSDT daily data for current year
  python scripts/fetch_and_ingest.py --symbol BTCUSDT --timeframe 1d

  # Fetch multiple symbols for historical years
  python scripts/fetch_and_ingest.py \\
      --symbol BTCUSDT,ETHUSDT,SOLUSDT --timeframe 1d --year 2024 --year 2025

  # Fetch and upload to pyne-worker /ingest endpoint
  python scripts/fetch_and_ingest.py \\
      --symbol BTCUSDT --timeframe 1d --ingest-url https://pyne-worker.example.com/ingest \\
      --api-key your-key-here

Output directory structure (matches R2 key format):
  data/{SYMBOL}/{TIMEFRAME}/{YYYY}.jsonl.gz
"""

from __future__ import annotations

import argparse
import gzip
import io
import json
import os
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime
from datetime import timezone
from typing import Any


# ---------------------------------------------------------------------------
# Binance API
# ---------------------------------------------------------------------------

_BINANCE_BASE = "https://api.binance.com"
_MAX_LIMIT = 1000  # max candles per API call

# Mapping from our timeframe format to Binance interval format
_BINANCE_INTERVALS: dict[str, str] = {
    "1m": "1m",
    "5m": "5m",
    "15m": "15m",
    "30m": "30m",
    "1h": "1h",
    "2h": "2h",
    "4h": "4h",
    "6h": "6h",
    "8h": "8h",
    "12h": "12h",
    "1d": "1d",
    "3d": "3d",
    "1w": "1w",
    "1M": "1M",
}


def fetch_klines(
    symbol: str,
    interval: str,
    start_time: int | None = None,
    end_time: int | None = None,
    limit: int = _MAX_LIMIT,
) -> list[dict[str, Any]]:
    """Fetch klines/candles from Binance spot API.

    Args:
        symbol: Trading pair (e.g. ``"BTCUSDT"``).
        interval: Binance interval string (e.g. ``"1d"``, ``"1h"``).
        start_time: Start time in ms epoch (optional).
        end_time: End time in ms epoch (optional).
        limit: Max candles per call (max 1000).

    Returns:
        List of dicts with ``time``, ``open``, ``high``, ``low``, ``close``,
        ``volume`` keys.
    """
    params: list[str] = [
        f"symbol={symbol.upper()}",
        f"interval={interval}",
        f"limit={limit}",
    ]
    if start_time is not None:
        params.append(f"startTime={start_time}")
    if end_time is not None:
        params.append(f"endTime={end_time}")

    url = f"{_BINANCE_BASE}/api/v3/klines?{'&'.join(params)}"

    try:
        req = urllib.request.Request(url, headers={"User-Agent": "pyne-worker/0.1"})
        with urllib.request.urlopen(req, timeout=30) as resp:
            data: list[list[Any]] = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        print(f"  HTTP {e.code}: {e.read().decode()}", file=sys.stderr)
        return []
    except (urllib.error.URLError, TimeoutError) as e:
        print(f"  Network error: {e}", file=sys.stderr)
        return []

    bars: list[dict[str, Any]] = []
    for k in data:
        bars.append(
            {
                "time": int(k[0]),
                "open": float(k[1]),
                "high": float(k[2]),
                "low": float(k[3]),
                "close": float(k[4]),
                "volume": float(k[5]),
            }
        )

    return bars


def fetch_all_klines(
    symbol: str,
    interval: str,
    start_time: int,
    end_time: int | None = None,
) -> list[dict[str, Any]]:
    """Fetch all klines in a time range using pagination.

    Args:
        symbol: Trading pair.
        interval: Binance interval.
        start_time: Start time in ms epoch.
        end_time: End time in ms epoch (default: now).

    Returns:
        Complete list of bars sorted by time.
    """
    all_bars: list[dict[str, Any]] = []
    current_start = start_time
    now_ms = int(time.time() * 1000)
    end = end_time or now_ms

    while current_start < end:
        bars = fetch_klines(symbol, interval, start_time=current_start, end_time=end)
        if not bars:
            break
        all_bars.extend(bars)
        # Advance to the next candle
        last_time = bars[-1]["time"]
        if last_time <= current_start:
            break  # prevent infinite loop
        current_start = last_time + 1
        # Rate limit: 1200 req/min = 50ms between calls
        time.sleep(0.05)

    return all_bars


# ---------------------------------------------------------------------------
# Output helpers
# ---------------------------------------------------------------------------


def write_jsonl_gz(bars: list[dict[str, Any]], output_path: str) -> int:
    """Write bars to a gzipped JSONL file.

    Args:
        bars: List of bar dicts.
        output_path: Destination file path.

    Returns:
        Number of bars written.
    """
    os.makedirs(os.path.dirname(output_path), exist_ok=True)
    buf = io.BytesIO()
    with gzip.GzipFile(fileobj=buf, mode="wb") as f:
        for bar in bars:
            f.write((json.dumps(bar, separators=(",", ":")) + "\n").encode("utf-8"))
    with open(output_path, "wb") as f:
        f.write(buf.getvalue())
    return len(bars)


def upload_via_ingest(
    bars: list[dict[str, Any]],
    symbol: str,
    timeframe: str,
    ingest_url: str,
    api_key: str,
) -> bool:
    """Upload bars to the pyne-worker /ingest endpoint.

    Args:
        bars: List of bar dicts.
        symbol: Trading pair.
        timeframe: Interval string.
        ingest_url: Full URL of the ingest endpoint.
        api_key: API key for authentication.

    Returns:
        True if upload succeeded.
    """
    payload = {
        "symbol": symbol.upper(),
        "timeframe": timeframe,
        "bars": bars,
    }
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        ingest_url,
        data=data,
        headers={
            "Content-Type": "application/json",
            "X-API-Key": api_key,
            "User-Agent": "pyne-worker/0.1",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            result = json.loads(resp.read().decode("utf-8"))
            ingested = result.get("ingested", 0)
            print(f"  Uploaded {ingested} bars to {ingest_url}")
            return True
    except urllib.error.HTTPError as e:
        print(f"  Upload failed (HTTP {e.code}): {e.read().decode()}", file=sys.stderr)
        return False
    except (urllib.error.URLError, TimeoutError) as e:
        print(f"  Upload failed: {e}", file=sys.stderr)
        return False


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Fetch OHLCV data from Binance and write to gzipped JSONL.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument(
        "--symbol",
        default="BTCUSDT",
        help="Trading pair(s), comma-separated (default: BTCUSDT)",
    )
    parser.add_argument(
        "--timeframe",
        default="1d",
        choices=list(_BINANCE_INTERVALS.keys()),
        help="Timeframe interval (default: 1d)",
    )
    parser.add_argument(
        "--year",
        action="append",
        type=int,
        dest="years",
        help="Year(s) to fetch (default: current year). Can be specified multiple times.",
    )
    parser.add_argument(
        "--output-dir",
        default="data",
        help="Output directory for gzipped JSONL files (default: data/)",
    )
    parser.add_argument(
        "--ingest-url",
        default=None,
        help="pyne-worker /ingest URL to upload directly (optional)",
    )
    parser.add_argument(
        "--api-key",
        default=None,
        help="API key for /ingest endpoint (required with --ingest-url)",
    )
    parser.add_argument(
        "--start-ms",
        type=int,
        default=None,
        help="Start time in ms epoch (overrides --year)",
    )
    parser.add_argument(
        "--end-ms",
        type=int,
        default=None,
        help="End time in ms epoch (default: now)",
    )
    return parser.parse_args(argv)


def main() -> None:
    args = parse_args()

    symbols = [s.strip().upper() for s in args.symbol.split(",")]
    binance_interval = _BINANCE_INTERVALS[args.timeframe]

    # Determine time range
    if args.start_ms is not None:
        start_ms = args.start_ms
    elif args.years:
        # Use the earliest year
        earliest_year = min(args.years)
        start_dt = datetime(earliest_year, 1, 1, tzinfo=timezone.utc)
        start_ms = int(start_dt.timestamp() * 1000)
    else:
        # Default: start of current year
        now = datetime.now(timezone.utc)
        start_dt = datetime(now.year, 1, 1, tzinfo=timezone.utc)
        start_ms = int(start_dt.timestamp() * 1000)

    end_ms = args.end_ms  # None = now

    total_bars = 0
    for symbol in symbols:
        print(f"Fetching {symbol} ({binance_interval})...")
        bars = fetch_all_klines(symbol, binance_interval, start_ms, end_ms)
        if not bars:
            print(f"  No data for {symbol}")
            continue
        print(f"  Got {len(bars)} bars")

        # Group bars by year for output
        by_year: dict[int, list[dict[str, Any]]] = {}
        for bar in bars:
            t = bar["time"]
            year = datetime.fromtimestamp(t / 1000, tz=timezone.utc).year
            by_year.setdefault(year, []).append(bar)

        for year, year_bars in by_year.items():
            # Write to local file
            output_path = os.path.join(args.output_dir, symbol, args.timeframe, f"{year}.jsonl.gz")
            n = write_jsonl_gz(year_bars, output_path)
            total_bars += n
            print(f"  Wrote {n} bars to {output_path}")

        # Upload via /ingest if configured
        if args.ingest_url:
            if not args.api_key:
                print("  Skipping upload: --api-key required with --ingest-url", file=sys.stderr)
            else:
                upload_via_ingest(bars, symbol, args.timeframe, args.ingest_url, args.api_key)

    print(f"\nDone. Total bars fetched: {total_bars}")


if __name__ == "__main__":
    main()
