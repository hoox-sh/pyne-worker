# Copyright (c) 2026 HOOX · PYNE · jango-blockchained
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Trade event forwarding — maps StrategyEvent records to trade-worker WebhookPayloads.

Usage:
  >>> from trade_forwarder import forward_events
  >>> result = await forward_events(
  ...     events, trade_service, symbol="BTCUSDT",
  ...     internal_auth_key="...", exchange="binance",
  ... )
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

import hashlib
import json
import math
import re
from typing import Any

# Kinds that map to trade-worker webhook actions (require positive qty).
_ACTIONABLE_KINDS = frozenset({"entry", "close", "exit", "close_all", "order"})
_EXCHANGE_RE = re.compile(r"^[a-zA-Z0-9_]+$")


def normalize_exchange(value: str | None, default: str = "binance") -> str:
    """Normalize exchange id to lowercase alphanumeric/underscore string."""
    fallback = (default or "binance").strip().lower()
    if not fallback or not _EXCHANGE_RE.match(fallback):
        fallback = "binance"
    if value is None or not isinstance(value, str):
        return fallback
    cleaned = re.sub(r"[^a-zA-Z0-9_]", "", value.strip()).lower()
    return cleaned or fallback


def _positive_qty(raw: Any) -> float | None:
    """Return positive finite qty or None if missing/invalid."""
    if raw is None or raw is False:
        return None
    try:
        qty = float(raw)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(qty) or qty <= 0:
        return None
    return qty


def _optional_price(event: dict[str, Any]) -> float | None:
    for key in ("price", "limit", "stop"):
        raw = event.get(key)
        if raw is None:
            continue
        try:
            price = float(raw)
        except (TypeError, ValueError):
            continue
        if math.isfinite(price) and price > 0:
            return price
    return None


def _optional_order_type(event: dict[str, Any]) -> str | None:
    raw = event.get("orderType") if event.get("orderType") is not None else event.get("order_type")
    if raw is None:
        return None
    s = str(raw).strip()
    return s or None


def _optional_leverage(
    event: dict[str, Any],
    default_leverage: int | None,
) -> int | None:
    raw = event.get("leverage")
    if raw is None:
        raw = default_leverage
    if raw is None:
        return None
    try:
        lev = int(raw)
    except (TypeError, ValueError):
        return None
    if lev <= 0:
        return None
    return lev


def _bar_label(event: dict[str, Any]) -> str:
    return str(event.get("bar_index", "?"))


def _idempotency_key(
    event: dict[str, Any],
    symbol: str,
    payload: dict[str, Any],
) -> str:
    """Stable Idempotency-Key per event (+ action for multi-payload kinds)."""
    script_id = event.get("deployed_script_id") or event.get("script_id") or ""
    kind = event.get("kind") or ""
    direction = event.get("direction") or ""
    action = payload.get("action") or ""
    bar_index = event.get("bar_index")
    bar_time = event.get("time") if event.get("time") is not None else event.get("bar_time")

    if script_id or kind or bar_index is not None or bar_time is not None:
        return (
            f"pyne:{script_id}:{symbol}:{kind}:{direction}:{bar_index}:{bar_time}:{action}"
        )

    raw = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
    digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()[:32]
    return f"pyne:{digest}"


def _decorate_payload(
    base: dict[str, Any],
    event: dict[str, Any],
    *,
    default_leverage: int | None,
) -> dict[str, Any]:
    """Attach optional price / orderType / leverage when present."""
    payload = dict(base)
    price = _optional_price(event)
    if price is not None:
        payload["price"] = price
    order_type = _optional_order_type(event)
    if order_type is not None:
        payload["orderType"] = order_type
    leverage = _optional_leverage(event, default_leverage)
    if leverage is not None:
        payload["leverage"] = leverage
    return payload


def _map_event_to_payload(
    event: dict[str, Any],
    symbol: str,
    *,
    exchange: str = "binance",
    default_leverage: int | None = None,
) -> list[dict[str, Any]] | dict[str, Any] | None:
    """Map a StrategyEvent dict to trade-worker WebhookPayload(s).

    Args:
        event: StrategyEvent as dict (from ``to_dict()``).
        symbol: Trading pair, e.g. ``"BTCUSDT"``.
        exchange: Default exchange when event has none.
        default_leverage: Optional leverage when event has none.

    Returns:
        One payload dict, a list of payloads (``close_all`` → CLOSE_LONG +
        CLOSE_SHORT), or ``None`` if the event is not actionable / invalid.
        Callers should treat missing qty on actionable kinds as a failure
        before relying on this alone — see :func:`forward_events`.
    """
    kind = event.get("kind", "") or ""
    if kind not in _ACTIONABLE_KINDS:
        return None

    qty = _positive_qty(event.get("qty"))
    if qty is None:
        # Signal invalid qty via empty list? Prefer None; forward_events
        # checks qty for actionable kinds and records "missing qty".
        return None

    exch = normalize_exchange(
        str(event["exchange"]) if event.get("exchange") is not None else None,
        default=exchange,
    )
    base = {
        "exchange": exch,
        "symbol": symbol,
        "quantity": qty,
    }
    direction = (event.get("direction") or "").strip().lower()

    if kind == "entry":
        if direction == "long":
            return _decorate_payload({**base, "action": "LONG"}, event, default_leverage=default_leverage)
        if direction == "short":
            return _decorate_payload({**base, "action": "SHORT"}, event, default_leverage=default_leverage)
        return None

    if kind in ("close", "exit"):
        if direction == "short":
            return _decorate_payload(
                {**base, "action": "CLOSE_SHORT"},
                event,
                default_leverage=default_leverage,
            )
        # long or unspecified → CLOSE_LONG (exit without direction)
        return _decorate_payload(
            {**base, "action": "CLOSE_LONG"},
            event,
            default_leverage=default_leverage,
        )

    if kind == "close_all":
        # Flatten both sides — trade-worker has no single CLOSE_ALL action.
        return [
            _decorate_payload(
                {**base, "action": "CLOSE_LONG"},
                event,
                default_leverage=default_leverage,
            ),
            _decorate_payload(
                {**base, "action": "CLOSE_SHORT"},
                event,
                default_leverage=default_leverage,
            ),
        ]

    if kind == "order":
        if direction == "long":
            return _decorate_payload({**base, "action": "LONG"}, event, default_leverage=default_leverage)
        if direction == "short":
            return _decorate_payload({**base, "action": "SHORT"}, event, default_leverage=default_leverage)
        return None

    return None


def _flatten_payloads(
    mapped: list[dict[str, Any]] | dict[str, Any] | None,
) -> list[dict[str, Any]]:
    if mapped is None:
        return []
    if isinstance(mapped, list):
        return [p for p in mapped if isinstance(p, dict)]
    if isinstance(mapped, dict):
        return [mapped]
    return []


async def forward_events(
    events: list[dict[str, Any]],
    trade_service: Any,
    symbol: str = "BTCUSDT",
    *,
    internal_auth_key: str | None = None,
    exchange: str = "binance",
    default_leverage: int | None = None,
) -> dict[str, Any]:
    """Forward actionable strategy events to trade-worker via service binding.

    Args:
        events: List of StrategyEvent dicts.
        trade_service: ``TRADE_SERVICE`` binding (``self.env.TRADE_SERVICE``).
        symbol: Trading pair.
        internal_auth_key: Mesh key for ``X-Internal-Auth-Key`` (required to
            POST; when missing, actionable events fail with a clear error
            instead of a silent 401).
        exchange: Default exchange id (event.exchange overrides).
        default_leverage: Optional default leverage for payloads.

    Returns:
        Dict with ``forwarded`` count, ``failed`` count, and ``errors`` list.
    """
    result: dict[str, Any] = {"forwarded": 0, "failed": 0, "errors": []}
    default_exchange = normalize_exchange(exchange)

    # Build work items first so we can fail closed without POSTing when auth is missing.
    work: list[tuple[dict[str, Any], list[dict[str, Any]]]] = []

    for event in events:
        if not isinstance(event, dict):
            continue
        kind = event.get("kind", "") or ""
        if kind not in _ACTIONABLE_KINDS:
            continue

        if _positive_qty(event.get("qty")) is None:
            result["failed"] += 1
            result["errors"].append(f"bar {_bar_label(event)}: missing qty")
            continue

        mapped = _map_event_to_payload(
            event,
            symbol,
            exchange=default_exchange,
            default_leverage=default_leverage,
        )
        payloads = _flatten_payloads(mapped)
        if not payloads:
            # Actionable kind but unmapped (e.g. entry without direction)
            continue

        work.append((event, payloads))

    if not work:
        return result

    auth_key = (internal_auth_key or "").strip() or None
    if not auth_key:
        n = sum(len(payloads) for _, payloads in work)
        result["failed"] += n
        result["errors"].append(
            "missing internal auth key: set INTERNAL_KEY_BINDING "
            "(or TRADE_EXECUTE_KEY_BINDING / TRADE_INTERNAL_KEY) to match "
            "trade-worker mesh auth; refusing to POST /webhook"
        )
        return result

    for event, payloads in work:
        for payload in payloads:
            try:
                body = json.dumps(payload).encode("utf-8")
                headers = {
                    "Content-Type": "application/json",
                    "X-Source": "pyne-worker",
                    "X-Internal-Auth-Key": auth_key,
                    "Idempotency-Key": _idempotency_key(event, symbol, payload),
                }
                response = await trade_service.fetch(
                    "/webhook",
                    method="POST",
                    headers=headers,
                    body=body,
                )

                # 200 = executed; 409 = entry-level idempotency duplicate (OK)
                if response.status in (200, 409):
                    result["forwarded"] += 1
                    if response.status == 409:
                        result.setdefault("deduplicated", 0)
                        result["deduplicated"] = int(result["deduplicated"]) + 1
                else:
                    result["failed"] += 1
                    action = payload.get("action", "?")
                    result["errors"].append(
                        f"bar {_bar_label(event)} ({action}): "
                        f"trade-worker returned {response.status}"
                    )
            except Exception as e:
                result["failed"] += 1
                action = payload.get("action", "?")
                result["errors"].append(f"bar {_bar_label(event)} ({action}): {e!s}")

    return result
