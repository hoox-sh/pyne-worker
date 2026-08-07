# Copyright (c) 2026 HOOX · PYNE · jango-blockchained
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Live market data feed for pyne-worker.

Fetches recent OHLCV from public exchange APIs that work from Cloudflare
Workers (Binance blocks many CF egress IPs; Bybit is the primary source).

Used by the 1-minute cron so R2 stays fresh without an external laptop feeder.
"""

from __future__ import annotations

import json
import time
from typing import Any
from typing import Awaitable
from typing import Callable
from typing import Protocol
from urllib.parse import quote

from data_provider import ingest_ohlcv_to_r2
from security import sanitize_symbol
from security import sanitize_timeframe

# Pine / Binance-style TF → milliseconds
_TF_MS: dict[str, int] = {
    "1m": 60_000,
    "3m": 180_000,
    "5m": 300_000,
    "15m": 900_000,
    "30m": 1_800_000,
    "1h": 3_600_000,
    "2h": 7_200_000,
    "4h": 14_400_000,
    "6h": 21_600_000,
    "12h": 43_200_000,
    "1d": 86_400_000,
    "1w": 604_800_000,
}

# Pine TF → Bybit interval string
_BYBIT_INTERVAL: dict[str, str] = {
    "1m": "1",
    "3m": "3",
    "5m": "5",
    "15m": "15",
    "30m": "30",
    "1h": "60",
    "2h": "120",
    "4h": "240",
    "6h": "360",
    "12h": "720",
    "1d": "D",
    "1w": "W",
}

# Pine TF → Binance interval (fallback; often blocked on CF)
_BINANCE_INTERVAL: dict[str, str] = {
    "1m": "1m",
    "3m": "3m",
    "5m": "5m",
    "15m": "15m",
    "30m": "30m",
    "1h": "1h",
    "2h": "2h",
    "4h": "4h",
    "6h": "6h",
    "12h": "12h",
    "1d": "1d",
    "1w": "1w",
}

HttpGetJson = Callable[[str], Awaitable[Any]]


class SupportsText(Protocol):
    async def text(self) -> str: ...


async def default_http_get_json(url: str) -> Any:
    """GET JSON — Workers ``js.fetch`` when available, else urllib."""
    try:
        from js import fetch  # type: ignore[import-not-found]

        resp = await fetch(url)
        status = int(getattr(resp, "status", 0) or 0)
        text = await resp.text()
        if status and status >= 400:
            raise RuntimeError(f"HTTP {status}: {text[:200]}")
        return json.loads(text)
    except ImportError:
        pass

    # Local / pytest
    import urllib.error
    import urllib.request

    req = urllib.request.Request(url, headers={"User-Agent": "pyne-worker-feed/0.5"})
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"HTTP {e.code}: {body[:200]}") from e


def only_closed_bars(
    bars: list[dict[str, Any]],
    timeframe: str,
    *,
    now_ms: int | None = None,
) -> list[dict[str, Any]]:
    """Drop the still-forming candle (bar-close semantics)."""
    interval = _TF_MS.get(timeframe)
    if not interval:
        return bars
    now = now_ms if now_ms is not None else int(time.time() * 1000)
    return [b for b in bars if int(b.get("time") or 0) + interval <= now]


async def fetch_klines_bybit(
    symbol: str,
    timeframe: str,
    *,
    limit: int = 200,
    http_get_json: HttpGetJson = default_http_get_json,
    category: str = "spot",
) -> list[dict[str, Any]]:
    """Fetch OHLCV from Bybit v5 public kline API."""
    sym = sanitize_symbol(symbol)
    tf = sanitize_timeframe(timeframe)
    if sym is None:
        raise ValueError(f"Invalid symbol: {symbol!r}")
    if tf is None or tf not in _BYBIT_INTERVAL:
        raise ValueError(f"Unsupported timeframe for Bybit: {timeframe}")
    interval = _BYBIT_INTERVAL[tf]

    # Only allow known Bybit category tokens (prevent query injection)
    cat = category if category in ("spot", "linear", "inverse") else "spot"

    limit = max(1, min(int(limit), 1000))
    url = (
        "https://api.bybit.com/v5/market/kline"
        f"?category={quote(cat, safe='')}&symbol={quote(sym, safe='')}"
        f"&interval={quote(interval, safe='')}&limit={limit}"
    )
    data = await http_get_json(url)
    if not isinstance(data, dict):
        raise RuntimeError("Bybit response is not a JSON object")
    if data.get("retCode") not in (0, "0", None):
        raise RuntimeError(f"Bybit error: {data.get('retMsg') or data.get('retCode')}")

    raw_list = (data.get("result") or {}).get("list") or []
    bars: list[dict[str, Any]] = []
    for row in raw_list:
        # Bybit: [start, open, high, low, close, volume, turnover]
        if not isinstance(row, (list, tuple)) or len(row) < 6:
            continue
        bars.append(
            {
                "time": int(row[0]),
                "open": float(row[1]),
                "high": float(row[2]),
                "low": float(row[3]),
                "close": float(row[4]),
                "volume": float(row[5]),
            }
        )
    # Bybit returns newest-first
    bars.sort(key=lambda b: b["time"])
    return bars


async def fetch_klines_binance(
    symbol: str,
    timeframe: str,
    *,
    limit: int = 200,
    http_get_json: HttpGetJson = default_http_get_json,
) -> list[dict[str, Any]]:
    """Fetch OHLCV from Binance spot (often blocked on CF; used as fallback)."""
    sym = sanitize_symbol(symbol)
    tf = sanitize_timeframe(timeframe)
    if sym is None:
        raise ValueError(f"Invalid symbol: {symbol!r}")
    if tf is None or tf not in _BINANCE_INTERVAL:
        raise ValueError(f"Unsupported timeframe for Binance: {timeframe}")
    interval = _BINANCE_INTERVAL[tf]

    limit = max(1, min(int(limit), 1000))
    url = (
        "https://api.binance.com/api/v3/klines"
        f"?symbol={quote(sym, safe='')}&interval={quote(interval, safe='')}&limit={limit}"
    )
    data = await http_get_json(url)
    if not isinstance(data, list):
        raise RuntimeError(f"Binance unexpected response: {type(data)}")
    bars: list[dict[str, Any]] = []
    for row in data:
        if not isinstance(row, (list, tuple)) or len(row) < 6:
            continue
        bars.append(
            {
                "time": int(row[0]),
                "open": float(row[1]),
                "high": float(row[2]),
                "low": float(row[3]),
                "close": float(row[4]),
                "volume": float(row[5]),
            }
        )
    bars.sort(key=lambda b: b["time"])
    return bars


async def fetch_klines(
    symbol: str,
    timeframe: str,
    *,
    limit: int = 200,
    closed_only: bool = True,
    http_get_json: HttpGetJson = default_http_get_json,
    sources: list[str] | None = None,
) -> tuple[list[dict[str, Any]], str]:
    """Fetch klines trying sources in order.

    Returns ``(bars, source_name)``.
    """
    order = sources or ["bybit", "binance"]
    errors: list[str] = []
    for src in order:
        try:
            if src == "bybit":
                bars = await fetch_klines_bybit(
                    symbol, timeframe, limit=limit, http_get_json=http_get_json
                )
            elif src == "binance":
                bars = await fetch_klines_binance(
                    symbol, timeframe, limit=limit, http_get_json=http_get_json
                )
            else:
                continue
            if closed_only:
                bars = only_closed_bars(bars, timeframe)
            if bars:
                return bars, src
            errors.append(f"{src}: empty")
        except Exception as e:
            errors.append(f"{src}: {e!s}")
    raise RuntimeError("All market sources failed: " + "; ".join(errors))


async def refresh_pair_to_r2(
    r2_bucket: Any,
    symbol: str,
    timeframe: str,
    *,
    limit: int = 200,
    closed_only: bool = True,
    http_get_json: HttpGetJson = default_http_get_json,
) -> dict[str, Any]:
    """Fetch latest klines and merge into R2. Returns a status dict."""
    bars, source = await fetch_klines(
        symbol,
        timeframe,
        limit=limit,
        closed_only=closed_only,
        http_get_json=http_get_json,
    )
    ingested = await ingest_ohlcv_to_r2(r2_bucket, symbol, timeframe, bars)
    last_t = int(bars[-1]["time"]) if bars else 0
    return {
        "symbol": symbol.upper(),
        "timeframe": timeframe,
        "source": source,
        "fetched": len(bars),
        "ingested": ingested,
        "last_bar_time": last_t,
    }


async def refresh_pairs_for_jobs(
    r2_bucket: Any,
    jobs: list[dict[str, Any]],
    *,
    limit: int = 200,
    http_get_json: HttpGetJson = default_http_get_json,
) -> list[dict[str, Any]]:
    """Refresh unique (symbol, timeframe) pairs required by cron jobs."""
    pairs: list[tuple[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for job in jobs:
        if not job.get("enabled", True):
            continue
        sym = sanitize_symbol(str(job.get("symbol") or "BTCUSDT")) or "BTCUSDT"
        tf = sanitize_timeframe(str(job.get("timeframe") or "1m"))
        if tf is None or tf not in _TF_MS:
            continue
        key = (sym, tf)
        if key in seen:
            continue
        seen.add(key)
        pairs.append(key)

    results: list[dict[str, Any]] = []
    for sym, tf in pairs:
        try:
            info = await refresh_pair_to_r2(
                r2_bucket,
                sym,
                tf,
                limit=limit,
                closed_only=True,
                http_get_json=http_get_json,
            )
            info["status"] = "ok"
            results.append(info)
        except Exception as e:
            results.append(
                {
                    "symbol": sym,
                    "timeframe": tf,
                    "status": "error",
                    "error": str(e),
                }
            )
    return results
