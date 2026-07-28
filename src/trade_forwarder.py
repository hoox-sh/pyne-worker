# Copyright (c) 2026 HOOX · PYNE · jango-blockchained
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Trade event forwarding — maps StrategyEvent records to trade-worker WebhookPayloads.

Usage:
  >>> from trade_forwarder import forward_events
  >>> result = await forward_events(events, trade_service, symbol="BTCUSDT")
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


def _map_event_to_payload(event: dict[str, Any], symbol: str) -> dict[str, Any] | None:
    """Map a StrategyEvent dict to a trade-worker WebhookPayload.

    Args:
        event: StrategyEvent as dict (from ``to_dict()``).
        symbol: Trading pair, e.g. ``"BTCUSDT"``.

    Returns:
        WebhookPayload dict or ``None`` if the event is not actionable.
    """
    qty = event.get("qty") or 1.0
    base = {
        "exchange": "binance",
        "symbol": symbol,
        "quantity": qty,
    }

    kind = event.get("kind", "")
    direction = event.get("direction", "")

    if kind == "entry":
        if direction == "long":
            return {**base, "action": "LONG"}
        if direction == "short":
            return {**base, "action": "SHORT"}
        return None

    if kind in ("close", "exit"):
        if direction == "short":
            return {**base, "action": "CLOSE_SHORT"}
        return {**base, "action": "CLOSE_LONG"}

    if kind == "close_all":
        return {**base, "action": "CLOSE_LONG"}

    if kind == "order":
        price = event.get("limit") or event.get("stop") or None
        order_type = event.get("order_type") or None
        if direction == "long":
            payload: dict[str, Any] = {**base, "action": "LONG"}
            if price is not None:
                payload["price"] = price
            if order_type is not None:
                payload["orderType"] = order_type
            return payload
        if direction == "short":
            payload = {**base, "action": "SHORT"}
            if price is not None:
                payload["price"] = price
            if order_type is not None:
                payload["orderType"] = order_type
            return payload
        return None

    return None


async def forward_events(
    events: list[dict[str, Any]],
    trade_service: Any,
    symbol: str = "BTCUSDT",
) -> dict[str, Any]:
    """Forward actionable strategy events to trade-worker via service binding.

    Args:
        events: List of StrategyEvent dicts.
        trade_service: ``TRADE_SERVICE`` binding (``self.env.TRADE_SERVICE``).
        symbol: Trading pair.

    Returns:
        Dict with ``forwarded`` count, ``failed`` count, and ``errors`` list.
    """
    result: dict[str, Any] = {"forwarded": 0, "failed": 0, "errors": []}

    for event in events:
        payload = _map_event_to_payload(event, symbol)
        if payload is None:
            continue

        try:
            body = json.dumps(payload).encode("utf-8")
            response = await trade_service.fetch(
                "/webhook",
                method="POST",
                headers={
                    "Content-Type": "application/json",
                    "X-Source": "pyne-worker",
                },
                body=body,
            )

            if response.status == 200:
                result["forwarded"] += 1
            else:
                result["failed"] += 1
                result["errors"].append(f"bar {event.get('bar_index', '?')}: trade-worker returned {response.status}")
        except Exception as e:
            result["failed"] += 1
            result["errors"].append(f"bar {event.get('bar_index', '?')}: {e!s}")

    return result
