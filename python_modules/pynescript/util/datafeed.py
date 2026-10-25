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

"""Realtime data feeds using CCXT Pro (WebSocket streaming).

This module provides async data feeds for live market data from 100+ crypto
exchanges via CCXT Pro:

- OHLCV candles (real-time)
- Trades
- Ticker
- Order book

CCXT Pro is the WebSocket/realtime counterpart to the historical CCXTProvider
in data.py.

Requires:
    pip install ccxt

Usage:
    from pynescript.util.datafeed import get_datafeed

    async def main():
        feed = get_datafeed("ccxtpro", exchange="binance")
        async with feed:
            async for candle in feed.watch_ohlcv("BTC/USDT", "1m"):
                print(candle)
                break  # process one update

    import asyncio
    asyncio.run(main())

Notes:
- All methods are async generators or coroutines.
- The underlying CCXT Pro exchange handles reconnection and backoff.
- Use one CCXTProDataFeed per exchange (it can watch multiple symbols).
- Not all exchanges support all watch_* methods. See https://docs.ccxt.com/

This complements the historical DataProvider in data.py and can be used to
power live evaluation, request.security simulation, or custom strategies.
"""

from __future__ import annotations

import asyncio
import logging
import random
import time

from abc import ABC
from abc import abstractmethod
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Any

from pynescript import stats as _stats
from pynescript.debug import scope_enabled as _debug_scope
from pynescript.debug import scoped_debug


_log = logging.getLogger(__name__)

#: Emit one debug line for this module when ``PYNE_DEBUG`` is enabled.
_dbg = scoped_debug(__name__, _log)


class DataFeedError(Exception):
    """Error raised by realtime data feeds."""


class DataFeed(ABC):
    """Abstract base class for realtime data feeds."""

    @abstractmethod
    async def watch_ohlcv(
        self,
        symbol: str,
        timeframe: str = "1m",
        limit: int | None = None,
    ) -> AsyncIterator[list[Any]]:
        """Stream real-time OHLCV candles.

        Args:
            symbol: Trading pair (e.g. "BTC/USDT")
            timeframe: Candle interval (e.g. "1m", "5m", "1h", "1d")
            limit: Optional number of candles per update

        Yields:
            List of [timestamp, open, high, low, close, volume] (or list of them)
        """
        ...

    @abstractmethod
    async def watch_trades(
        self,
        symbol: str,
        limit: int | None = None,
    ) -> AsyncIterator[dict[str, Any] | list[dict[str, Any]]]:
        """Stream real-time trades.

        Yields:
            Trade dict(s) with keys like 'id', 'timestamp', 'price', 'amount', 'side', etc.
        """
        ...

    @abstractmethod
    async def watch_ticker(self, symbol: str) -> AsyncIterator[dict[str, Any]]:
        """Stream real-time ticker updates.

        Yields:
            Ticker dict with bid, ask, last, high, low, volume, etc.
        """
        ...

    @abstractmethod
    async def watch_order_book(self, symbol: str, limit: int = 20) -> AsyncIterator[dict[str, Any]]:
        """Stream real-time order book snapshots.

        Yields:
            Order book with 'bids', 'asks', 'timestamp', etc.
        """
        ...

    async def close(self) -> None:  # noqa: B027
        """Close the underlying connection(s)."""
        pass  # default no-op for subclasses that do not need explicit close

    def poll_ohlcv(self, symbol: str, timeframe: str = "1m") -> list[Any] | None:
        """Sync snapshot of the latest forming OHLCV, or ``None``.

        Returns ``[ts_ms, open, high, low, close, volume]``. Default is
        ``None`` (no-op) so async-only feeds do not block the bar loop.
        """
        return None

    async def __aenter__(self) -> DataFeed:
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        await self.close()


def poll_forming_ohlcv(
    data_feed: Any,
    symbol: str,
    timeframe: str = "1m",
) -> tuple[float, float, float, float, float, float] | None:
    """Sync poll → ``(time, open, high, low, close, volume)`` or ``None``.

    Shared by interpret ``_apply_feed_forming_bar`` and object-mode compile
    realtime ticks. Never raises; malformed candles and missing ``poll_ohlcv``
    are no-ops.
    """
    if data_feed is None:
        return None
    poll = getattr(data_feed, "poll_ohlcv", None)
    if not callable(poll):
        return None
    try:
        candle = poll(symbol or "", timeframe)
    except Exception:
        return None
    if not candle:
        return None
    try:
        if isinstance(candle[0], (list, tuple)):
            candle = candle[-1]
        ts = float(int(candle[0]))
        o = float(candle[1])
        h = float(candle[2])
        low = float(candle[3])
        c = float(candle[4])
        vol = float(candle[5]) if len(candle) > 5 else 0.0
    except (TypeError, ValueError, IndexError):
        return None
    return (ts, o, h, low, c, vol)


class CCXTProDataFeed(DataFeed):
    """Real-time data feed powered by CCXT Pro WebSockets.

    Supports the same exchanges as the historical CCXTProvider plus realtime
    capabilities on supported venues (Binance, Bybit, OKX, Coinbase, Kraken, etc.).

    Example:
        feed = CCXTProDataFeed(exchange="binance")
        async with feed:
            async for candle in feed.watch_ohlcv("BTC/USDT", "1m"):
                ...
    """

    def __init__(
        self,
        exchange: str = "binance",
        api_key: str = "",
        secret: str = "",
        password: str = "",
        sandbox: bool = False,  # CLI/ factory friendly
        **options: Any,
    ) -> None:
        """Initialize the CCXT Pro feed.

        Args:
            exchange: Exchange id (binance, bybit, okx, coinbase, kraken, ...)
            api_key: Optional API key for private streams
            secret: Optional API secret
            password: Optional passphrase (required by some exchanges)
            sandbox: Use testnet/sandbox if supported
            **options: Extra options passed to the CCXT Pro exchange constructor
        """
        self._exchange_name = exchange.lower()
        self._api_key = api_key
        self._secret = secret
        self._password = password
        self._sandbox = sandbox
        self._options = options
        self._exchange: Any | None = None
        self._ohlcv_cache: dict[tuple[str, str], list[Any]] = {}
        _dbg(
            "CCXTProDataFeed init exchange=%s sandbox=%s has_key=%s options=%s",
            self._exchange_name,
            sandbox,
            bool(api_key),
            sorted(options),
        )
        _stats.count("datafeed.feeds_created", dim="ccxtpro")

    async def _get_exchange(self) -> Any:
        """Lazily create the CCXT Pro exchange instance."""
        if self._exchange is None:
            _t_ex0 = time.perf_counter()
            _dbg("connecting ccxt.pro exchange=%s", self._exchange_name)
            try:
                import ccxt.pro as ccxtpro  # type: ignore[import-not-found]  # lazy to avoid hard dep
            except ImportError as e:
                msg = "ccxt is required for CCXTProDataFeed. Install with: pip install ccxt"
                _dbg("ccxt.pro unavailable: %s", e)
                _stats.count("datafeed.errors", dim="ccxt_missing")
                raise DataFeedError(msg) from e

            exchange_class = getattr(ccxtpro, self._exchange_name, None)
            if exchange_class is None:
                msg = (
                    f"Exchange '{self._exchange_name}' not found in ccxt.pro. "
                    "Check available exchanges with ccxt.pro.exchanges"
                )
                _dbg("unknown exchange=%s", self._exchange_name)
                _stats.count("datafeed.errors", dim="unknown_exchange")
                raise DataFeedError(msg)

            params: dict[str, Any] = dict(self._options)
            if self._api_key:
                params["apiKey"] = self._api_key
            if self._secret:
                params["secret"] = self._secret
            if self._password:
                params["password"] = self._password
            if self._sandbox:
                params["sandbox"] = True
            if self._exchange_name == "binance":
                opts = params.get("options") if isinstance(params.get("options"), dict) else {}
                default = str(opts.get("defaultType") or "spot")
                opts = {**opts, "defaultType": default, "fetchMarkets": [default]}
                params["options"] = opts

            self._exchange = exchange_class(params)
            from pynescript.util.data import tune_ccxt_public_urls

            tune_ccxt_public_urls(self._exchange, self._exchange_name)
            _dbg(
                "ccxt.pro exchange ready exchange=%s in %.1fms sandbox=%s",
                self._exchange_name,
                (time.perf_counter() - _t_ex0) * 1000.0,
                self._sandbox,
            )
            _stats.count("datafeed.exchanges_connected", dim=self._exchange_name)
            _stats.latency("datafeed.connect", time.perf_counter() - _t_ex0)

        return self._exchange

    def _cache_ohlcv_bars(self, symbol: str, timeframe: str, ohlcv: Any) -> None:
        """Store the last candle from a watch/fetch payload (sync, never raises)."""
        if not ohlcv:
            return
        candle: Any = ohlcv
        try:
            if isinstance(ohlcv[0], (list, tuple)):
                candle = ohlcv[-1]
        except (TypeError, IndexError):
            return
        try:
            cached = [
                int(candle[0]),
                float(candle[1]),
                float(candle[2]),
                float(candle[3]),
                float(candle[4]),
                float(candle[5]) if len(candle) > 5 else 0.0,
            ]
        except (TypeError, ValueError, IndexError):
            return
        key = (str(symbol or ""), str(timeframe or "1m"))
        self._ohlcv_cache[key] = cached

    def poll_ohlcv(self, symbol: str, timeframe: str = "1m") -> list[Any] | None:
        """Last cached watch/fetch candle for *symbol*/*timeframe*, or ``None``.

        Sync and network-free: the bar loop must not open sockets. A live
        ``watch_ohlcv`` (or ``fetch_latest_ohlcv``) fills the cache; until then
        this is a no-op like the ABC default.
        """
        key = (str(symbol or ""), str(timeframe or "1m"))
        cached = self._ohlcv_cache.get(key)
        if cached is None:
            return None
        return list(cached)

    async def supports_for_symbols(self) -> bool:
        """True when the venue implements ``watchOHLCVForSymbols`` (fan-in)."""
        try:
            exchange = await self._get_exchange()
        except Exception:
            return False
        has = getattr(exchange, "has", None)
        if isinstance(has, dict):
            return bool(has.get("watchOHLCVForSymbols"))
        return hasattr(exchange, "watchOHLCVForSymbols")

    async def watch_ohlcv_for_symbols(
        self,
        symbols: list[tuple[str, str]],
    ) -> AsyncIterator[tuple[str, str, list[Any]]]:
        """Fan-in stream: ``(symbol, timeframe, candles)`` per update.

        Raises :class:`DataFeedError` when the venue has no
        ``watchOHLCVForSymbols`` — callers fall back to per-series watches.
        """
        exchange = await self._get_exchange()
        impl = getattr(exchange, "watchOHLCVForSymbols", None)
        if impl is None:
            msg = f"{self._exchange_name} has no watchOHLCVForSymbols"
            raise DataFeedError(msg)
        _stats.count("datafeed.streams", dim="ohlcv_for_symbols")
        while True:
            try:
                grouped = await impl([[s, tf] for s, tf in symbols])
                if isinstance(grouped, dict):
                    for sym, per_tf in grouped.items():
                        if isinstance(per_tf, dict):
                            for tf, candles in per_tf.items():
                                if candles:
                                    self._cache_ohlcv_bars(str(sym), str(tf), candles)
                                    yield (sym, tf, candles)
                        elif per_tf:
                            self._cache_ohlcv_bars(str(sym), "1m", per_tf)
                            yield (sym, "", per_tf)
                elif grouped:
                    for sym, tf in symbols:
                        self._cache_ohlcv_bars(str(sym), str(tf), grouped)
                        yield (sym, tf, grouped)
            except Exception as e:
                if "closed" in str(e).lower() or "connection" in str(e).lower():
                    await asyncio.sleep(1)
                    continue
                msg = f"watch_ohlcv_for_symbols error: {e}"
                raise DataFeedError(msg) from e

    async def watch_ohlcv(
        self,
        symbol: str,
        timeframe: str = "1m",
        limit: int | None = None,
    ) -> AsyncIterator[list[Any]]:
        """Stream OHLCV updates using watch_ohlcv."""
        exchange = await self._get_exchange()
        _dbg(
            "watch_ohlcv subscribe exchange=%s symbol=%s timeframe=%s limit=%s",
            self._exchange_name,
            symbol,
            timeframe,
            limit,
        )
        _stats.count("datafeed.streams", dim="ohlcv")
        n_updates = 0
        while True:
            try:
                ohlcv = await exchange.watch_ohlcv(symbol, timeframe, limit=limit)
                if ohlcv:
                    self._cache_ohlcv_bars(symbol, timeframe, ohlcv)
                    n_updates += 1
                    if _debug_scope(__name__) and n_updates <= 3:
                        # First few only — steady state would flood the log.
                        _dbg(
                            "watch_ohlcv update #%d exchange=%s symbol=%s bars=%s last_ts=%s",
                            n_updates,
                            self._exchange_name,
                            symbol,
                            len(ohlcv),
                            ohlcv[-1][0] if ohlcv else None,
                        )
                    elif n_updates == 4:
                        _dbg("watch_ohlcv further updates suppressed for %s %s", self._exchange_name, symbol)
                    yield ohlcv
            except Exception as e:  # broad to let ccxt handle reconnect logic
                # Most pro implementations recover internally on next call
                # Yielding error info is optional; here we just continue
                if "closed" in str(e).lower() or "connection" in str(e).lower():
                    _dbg("watch_ohlcv reconnect after %s", e)
                    _stats.count("datafeed.reconnects", dim="ohlcv")
                    await asyncio.sleep(1)
                    continue
                msg = f"watch_ohlcv error: {e}"
                _dbg("watch_ohlcv error %s %s: %s", self._exchange_name, symbol, e)
                _stats.count("datafeed.errors", dim="watch_ohlcv")
                raise DataFeedError(msg) from e

    async def watch_trades(
        self,
        symbol: str,
        limit: int | None = None,
    ) -> AsyncIterator[dict[str, Any] | list[dict[str, Any]]]:
        """Stream trades using watch_trades."""
        exchange = await self._get_exchange()
        _dbg("watch_trades subscribe exchange=%s symbol=%s limit=%s", self._exchange_name, symbol, limit)
        _stats.count("datafeed.streams", dim="trades")
        while True:
            try:
                trades = await exchange.watch_trades(symbol, limit=limit)
                if trades:
                    yield trades
            except Exception as e:
                if "closed" in str(e).lower() or "connection" in str(e).lower():
                    _dbg("watch_trades reconnect after %s", e)
                    _stats.count("datafeed.reconnects", dim="trades")
                    await asyncio.sleep(1)
                    continue
                msg = f"watch_trades error: {e}"
                _dbg("watch_trades error %s %s: %s", self._exchange_name, symbol, e)
                _stats.count("datafeed.errors", dim="watch_trades")
                raise DataFeedError(msg) from e

    async def watch_ticker(self, symbol: str) -> AsyncIterator[dict[str, Any]]:
        """Stream ticker updates using watch_ticker."""
        exchange = await self._get_exchange()
        _dbg("watch_ticker subscribe exchange=%s symbol=%s", self._exchange_name, symbol)
        _stats.count("datafeed.streams", dim="ticker")
        while True:
            try:
                ticker = await exchange.watch_ticker(symbol)
                yield ticker
            except Exception as e:
                if "closed" in str(e).lower() or "connection" in str(e).lower():
                    _dbg("watch_ticker reconnect after %s", e)
                    _stats.count("datafeed.reconnects", dim="ticker")
                    await asyncio.sleep(1)
                    continue
                msg = f"watch_ticker error: {e}"
                _dbg("watch_ticker error %s %s: %s", self._exchange_name, symbol, e)
                _stats.count("datafeed.errors", dim="watch_ticker")
                raise DataFeedError(msg) from e

    async def watch_order_book(self, symbol: str, limit: int = 20) -> AsyncIterator[dict[str, Any]]:
        """Stream order book using watch_order_book."""
        exchange = await self._get_exchange()
        _dbg("watch_order_book subscribe exchange=%s symbol=%s limit=%s", self._exchange_name, symbol, limit)
        _stats.count("datafeed.streams", dim="order_book")
        while True:
            try:
                ob = await exchange.watch_order_book(symbol, limit)
                yield ob
            except Exception as e:
                if "closed" in str(e).lower() or "connection" in str(e).lower():
                    _dbg("watch_order_book reconnect after %s", e)
                    _stats.count("datafeed.reconnects", dim="order_book")
                    await asyncio.sleep(1)
                    continue
                msg = f"watch_order_book error: {e}"
                _dbg("watch_order_book error %s %s: %s", self._exchange_name, symbol, e)
                _stats.count("datafeed.errors", dim="watch_order_book")
                raise DataFeedError(msg) from e

    async def close(self) -> None:
        """Close the exchange WebSocket connection."""
        if self._exchange is not None:
            _dbg("closing ccxt.pro exchange=%s", self._exchange_name)
            try:
                await self._exchange.close()
            except Exception:  # best effort shutdown
                pass
            self._exchange = None

    async def __aenter__(self) -> CCXTProDataFeed:
        _dbg("entering ccxt.pro feed exchange=%s", self._exchange_name)
        await self._get_exchange()
        return self

    async def __aexit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        await self.close()

    # --- Sync convenience methods for compatibility with sync evaluator / request.* ---
    def fetch_latest_ohlcv(self, symbol: str, timeframe: str = "1m", limit: int = 1) -> list[Any]:
        """Synchronous one-shot fetch of latest OHLCV (uses asyncio.run internally).

        Useful for request.security in a mostly-sync context.
        """
        return asyncio.run(self._fetch_latest_ohlcv_async(symbol, timeframe, limit))

    async def _fetch_latest_ohlcv_async(self, symbol: str, timeframe: str = "1m", limit: int = 1) -> list[Any]:
        exchange = await self._get_exchange()
        # Use watch once to get fresh data, fallback to fetch if needed
        try:
            ohlcv = await exchange.watch_ohlcv(symbol, timeframe, limit=limit)
            out = ohlcv[-limit:] if ohlcv else []
        except Exception:
            # Fallback to REST fetch_ohlcv for latest
            ohlcv = await exchange.fetch_ohlcv(symbol, timeframe, limit=limit)
            out = ohlcv or []
        if out:
            self._cache_ohlcv_bars(symbol, timeframe, out)
        return out

    def fetch_latest_ticker(self, symbol: str) -> dict[str, Any]:
        """Synchronous fetch of latest ticker."""
        return asyncio.run(self._fetch_latest_ticker_async(symbol))

    async def _fetch_latest_ticker_async(self, symbol: str) -> dict[str, Any]:
        exchange = await self._get_exchange()
        try:
            return await exchange.watch_ticker(symbol)
        except Exception:
            return await exchange.fetch_ticker(symbol) or {}


def get_datafeed(name: str = "ccxtpro", **kwargs: Any) -> DataFeed:
    """Factory for realtime data feeds.

    Args:
        name: "ccxtpro" | "mock"
        **kwargs: Passed to the feed constructor

    Returns:
        A DataFeed instance.
    """
    if name in ("ccxtpro", "ccxt", "pro"):
        _dbg("get_datafeed name=%s exchange=%s", name, kwargs.get("exchange", "binance"))
        return CCXTProDataFeed(
            exchange=kwargs.get("exchange", "binance"),
            api_key=kwargs.get("api_key", ""),
            secret=kwargs.get("secret", ""),
            password=kwargs.get("password", ""),
            sandbox=kwargs.get("sandbox", False),
            **{k: v for k, v in kwargs.items() if k not in {"exchange", "api_key", "secret", "password", "sandbox"}},
        )
    if name == "mock":
        _dbg("get_datafeed name=mock symbol=%s", kwargs.get("symbol", "BTC/USDT"))
        return MockDataFeed(**kwargs)

    msg = f"Unknown datafeed: {name}. Supported: 'ccxtpro', 'mock'"
    _dbg("get_datafeed unknown name=%s", name)
    _stats.count("datafeed.errors", dim="unknown_factory")
    raise DataFeedError(msg)


class MockDataFeed(DataFeed):
    """Mock realtime data feed for testing (no network required).

    Generates synthetic streaming data.
    """

    def __init__(
        self,
        symbol: str = "BTC/USDT",
        start_price: float = 25000.0,
        scripted: list[list[Any]] | None = None,
    ):
        self.symbol = symbol
        self.price = start_price
        self._closed = False
        self._scripted = [list(c) for c in scripted] if scripted is not None else None
        self._poll_i = 0
        _dbg("MockDataFeed init symbol=%s start_price=%s", symbol, start_price)
        _stats.count("datafeed.feeds_created", dim="mock")

    async def watch_ohlcv(
        self, symbol: str | None = None, timeframe: str = "1m", limit: int | None = None
    ) -> AsyncIterator[list[Any]]:
        _dbg("MockDataFeed.watch_ohlcv symbol=%s timeframe=%s", symbol or self.symbol, timeframe)
        _stats.count("datafeed.streams", dim="mock_ohlcv")
        ts = 1700000000000
        while not self._closed:
            self.price *= 1 + random.uniform(-0.001, 0.001)
            o = self.price
            h = o * (1 + random.uniform(0, 0.002))
            lo = o * (1 - random.uniform(0, 0.002))
            c = o * (1 + random.uniform(-0.001, 0.001))
            v = random.uniform(100, 1000)
            yield [ts, o, h, lo, c, v]
            ts += 60_000
            await asyncio.sleep(0.01)  # fast simulation

    async def watch_trades(
        self, symbol: str | None = None, limit: int | None = None
    ) -> AsyncIterator[dict[str, Any] | list[dict[str, Any]]]:
        sym = symbol or self.symbol
        while not self._closed:
            self.price *= 1 + random.uniform(-0.0005, 0.0005)
            yield {
                "symbol": sym,
                "price": self.price,
                "amount": random.uniform(0.1, 2.0),
                "side": random.choice(["buy", "sell"]),
                "timestamp": 1700000000000,
            }
            await asyncio.sleep(0.01)

    async def watch_ticker(self, symbol: str | None = None) -> AsyncIterator[dict[str, Any]]:
        sym = symbol or self.symbol
        while not self._closed:
            self.price *= 1 + random.uniform(-0.0003, 0.0003)
            yield {
                "symbol": sym,
                "last": self.price,
                "bid": self.price - 1,
                "ask": self.price + 1,
                "high": self.price * 1.01,
                "low": self.price * 0.99,
            }
            await asyncio.sleep(0.01)

    async def watch_order_book(self, symbol: str | None = None, limit: int = 20) -> AsyncIterator[dict[str, Any]]:
        sym = symbol or self.symbol
        while not self._closed:
            yield {
                "symbol": sym,
                "bids": [[self.price - i * 0.5, 1.0] for i in range(limit)],
                "asks": [[self.price + i * 0.5, 1.0] for i in range(limit)],
            }
            await asyncio.sleep(0.01)

    async def close(self) -> None:
        self._closed = True

    async def __aenter__(self) -> MockDataFeed:
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        await self.close()

    # Sync helpers used by request.* evaluator paths (no network)
    def fetch_latest_ticker(self, symbol: str | None = None) -> dict[str, Any]:
        sym = symbol or self.symbol
        return {
            "symbol": sym,
            "last": float(self.price),
            "close": float(self.price),
            "bid": float(self.price) - 1,
            "ask": float(self.price) + 1,
            "high": float(self.price) * 1.01,
            "low": float(self.price) * 0.99,
        }

    def fetch_latest_ohlcv(self, symbol: str | None = None, timeframe: str = "1m", limit: int = 5) -> list[list[Any]]:
        _ = symbol or self.symbol
        _ = timeframe
        ts = 1_700_000_000_000
        out: list[list[Any]] = []
        px = float(self.price)
        for i in range(max(1, int(limit))):
            o = px * (1 + 0.0001 * i)
            h = o * 1.001
            lo = o * 0.999
            c = o
            v = 100.0 + i
            out.append([ts + i * 60_000, o, h, lo, c, v])
        return out

    def poll_ohlcv(self, symbol: str, timeframe: str = "1m") -> list[Any] | None:
        """Next forming candle (sync, no sleep). ``scripted`` then ``None``."""
        _ = symbol
        _ = timeframe
        if self._scripted is not None:
            if self._poll_i >= len(self._scripted):
                return None
            candle = list(self._scripted[self._poll_i])
            self._poll_i += 1
            try:
                self.price = float(candle[4])
            except (TypeError, ValueError, IndexError):
                pass
            return candle
        i = self._poll_i
        self._poll_i += 1
        o = float(self.price)
        h = o + 1.0
        lo = o - 1.0
        c = o + 0.5
        v = 100.0 + i
        ts = 1_700_000_000_000 + i * 60_000
        self.price = c
        return [ts, o, h, lo, c, v]


# Composite / unified feed example
class CompositeDataFeed(DataFeed):
    """Unified datafeed that tries realtime first, falls back to other sources.

    Useful for hybrid historical + live usage.
    """

    def __init__(self, primary: DataFeed, fallback: DataFeed | None = None):
        self.primary = primary
        self.fallback = fallback

    async def watch_ohlcv(self, symbol: str, timeframe: str = "1m", limit: int | None = None):
        try:
            async for item in self.primary.watch_ohlcv(symbol, timeframe, limit):
                yield item
        except Exception:
            if self.fallback:
                # Note: fallback may be sync historical; here we just re-raise or yield mock
                async for item in self.fallback.watch_ohlcv(symbol, timeframe, limit):  # type: ignore[attr-defined]
                    yield item
            else:
                raise

    async def watch_trades(self, symbol: str, limit: int | None = None):
        async for item in self.primary.watch_trades(symbol, limit):
            yield item

    async def watch_ticker(self, symbol: str):
        async for item in self.primary.watch_ticker(symbol):
            yield item

    async def watch_order_book(self, symbol: str, limit: int = 20):
        async for item in self.primary.watch_order_book(symbol, limit):
            yield item

    async def close(self):
        await self.primary.close()
        if self.fallback and hasattr(self.fallback, "close"):
            await self.fallback.close()  # type: ignore[attr-defined]

    # Sync helpers for request.* evaluator (prefer primary, then fallback)
    def fetch_latest_ticker(self, symbol: str) -> dict[str, Any]:
        for src in (self.primary, self.fallback):
            if src is not None and hasattr(src, "fetch_latest_ticker"):
                try:
                    return src.fetch_latest_ticker(symbol)  # type: ignore[attr-defined]
                except Exception:  # noqa: S112 - fall through to next source
                    continue
        return {}

    def fetch_latest_ohlcv(self, symbol: str, timeframe: str = "1m", limit: int = 5) -> list[list[Any]]:
        for src in (self.primary, self.fallback):
            if src is not None and hasattr(src, "fetch_latest_ohlcv"):
                try:
                    return src.fetch_latest_ohlcv(symbol, timeframe, limit)  # type: ignore[attr-defined]
                except Exception:  # noqa: S112 - fall through to next source
                    continue
        return []

    def poll_ohlcv(self, symbol: str, timeframe: str = "1m") -> list[Any] | None:
        for src in (self.primary, self.fallback):
            if src is None:
                continue
            poll = getattr(src, "poll_ohlcv", None)
            if not callable(poll):
                continue
            try:
                candle = poll(symbol, timeframe)
            except Exception:  # noqa: S112 - fall through to next source
                continue
            if candle:
                return candle
        return None


# --- Order execution / position tracking on top of datafeed (point 3) ---


@dataclass
class Order:
    id: str
    symbol: str
    side: str  # "buy" / "sell"
    qty: float
    price: float | None = None  # None = market
    filled: bool = False
    avg_fill_price: float | None = None


class DataFeedBroker:
    """Simple paper-trading broker driven by a realtime DataFeed.

    Tracks positions and simulates fills using streaming prices from the feed.
    Can be used together with the Pine evaluator's strategy events for live simulation.
    """

    def __init__(self, feed: DataFeed, initial_balance: float = 100_000.0):
        self.feed = feed
        self.balance = initial_balance
        self.positions: dict[str, float] = {}  # symbol -> qty (positive long)
        self.orders: dict[str, Order] = {}
        self._order_counter = 0
        self._running = False

    def place_order(self, symbol: str, side: str, qty: float, price: float | None = None) -> str:
        """Place a limit or market order. Returns order id."""
        self._order_counter += 1
        oid = f"ord_{self._order_counter}"
        order = Order(id=oid, symbol=symbol, side=side, qty=qty, price=price)
        self.orders[oid] = order
        return oid

    async def run(self, symbols: list[str] | None = None) -> None:
        """Drive the broker using the datafeed (listens to tickers/trades for fills)."""
        self._running = True
        symbols = symbols or ["BTC/USDT"]
        try:
            async with self.feed:
                # Simple: use ticker stream to simulate price updates and fill orders
                async for ticker in self.feed.watch_ticker(symbols[0]):
                    if not self._running:
                        break
                    price = ticker.get("last") or ticker.get("bid") or ticker.get("ask")
                    if price is None:
                        continue
                    await self._process_fills(price)
        finally:
            self._running = False

    async def _process_fills(self, current_price: float) -> None:
        for _oid, order in list(self.orders.items()):
            if order.filled:
                continue
            fill = False
            if order.price is None:  # market
                fill = True
            elif order.side == "buy" and current_price <= order.price:
                fill = True
            elif order.side == "sell" and current_price >= order.price:
                fill = True

            if fill:
                cost = current_price * order.qty
                if order.side == "buy":
                    if self.balance >= cost:
                        self.balance -= cost
                        self.positions[order.symbol] = self.positions.get(order.symbol, 0) + order.qty
                else:
                    pos = self.positions.get(order.symbol, 0)
                    if pos >= order.qty:
                        self.balance += cost
                        self.positions[order.symbol] = pos - order.qty
                order.filled = True
                order.avg_fill_price = current_price
                # In real integration you would emit StrategyEvent here

    def get_position(self, symbol: str) -> float:
        return self.positions.get(symbol, 0.0)

    def get_balance(self) -> float:
        return self.balance

    async def stop(self) -> None:
        self._running = False
        await self.feed.close()


__all__ = [
    "CCXTProDataFeed",
    "CompositeDataFeed",
    "DataFeed",
    "DataFeedBroker",
    "DataFeedError",
    "MockDataFeed",
    "get_datafeed",
]
