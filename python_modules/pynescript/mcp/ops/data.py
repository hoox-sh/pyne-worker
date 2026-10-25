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

"""Market data fetch (CLI ``pyne data``). Default provider is mock (no network)."""

from __future__ import annotations

from typing import Any

from pynescript.mcp.serialize import BARS_MAX
from pynescript.mcp.serialize import fail
from pynescript.mcp.serialize import jsonable
from pynescript.mcp.serialize import ok


def fetch_data(
    symbol: str,
    *,
    provider: str = "mock",
    period: str = "1y",
    interval: str = "1d",
    api_key: str = "",
    secret: str = "",
    exchange: str = "binance",
    include_series: bool = True,
) -> dict[str, Any]:
    """Fetch OHLCV. Non-mock providers hit the network."""
    from pynescript.util.data import DataProviderError
    from pynescript.util.data import get_provider

    name = (provider or "mock").lower()
    kwargs: dict[str, Any] = {}
    if name == "alphavantage":
        kwargs["api_key"] = api_key or "demo"
    elif name == "ccxt":
        kwargs["exchange"] = exchange
        if api_key:
            kwargs["api_key"] = api_key
        if secret:
            kwargs["secret"] = secret
    try:
        prov = get_provider(name, **kwargs)
        result = prov.fetch(symbol, period, interval)
    except DataProviderError as exc:
        return fail(str(exc), kind="network")
    except Exception as exc:
        return fail(f"{type(exc).__name__}: {exc}", kind="network")
    closes = list(result.get("close") or [])
    n = len(closes)
    if n == 0:
        return fail("provider returned 0 bars", kind="data")
    payload: dict[str, Any] = {
        "symbol": result.get("symbol", symbol),
        "provider": name,
        "period": period,
        "interval": interval,
        "bars": n,
    }
    if include_series:
        cap = min(n, BARS_MAX)
        payload["open"] = jsonable(list(result["open"])[:cap])
        payload["high"] = jsonable(list(result["high"])[:cap])
        payload["low"] = jsonable(list(result["low"])[:cap])
        payload["close"] = jsonable(closes[:cap])
        payload["volume"] = jsonable(list(result.get("volume") or [])[:cap])
    return ok(**payload)
