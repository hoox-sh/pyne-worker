# Copyright (c) 2026 HOOX · PYNE · jango-blockchained
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Unit tests for strategy event → trade-worker webhook mapping."""

from __future__ import annotations

import asyncio
from typing import Any

from trade_forwarder import _map_event_to_payload
from trade_forwarder import forward_events
from trade_forwarder import normalize_exchange


class FakeTradeService:
    """Records fetch calls and returns configurable status codes."""

    def __init__(self, status: int = 200) -> None:
        self.status = status
        self.calls: list[dict[str, Any]] = []

    async def fetch(
        self,
        path: str,
        *,
        method: str = "GET",
        headers: dict[str, str] | None = None,
        body: bytes | None = None,
    ) -> Any:
        self.calls.append(
            {
                "path": path,
                "method": method,
                "headers": dict(headers or {}),
                "body": body,
            }
        )

        class _Resp:
            def __init__(self, st: int) -> None:
                self.status = st

        return _Resp(self.status)


def _ev(**kwargs: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "kind": "entry",
        "direction": "long",
        "qty": 0.01,
        "bar_index": 1,
        "bar_time": 1_700_000_000_000,
        "script_id": "s1",
    }
    base.update(kwargs)
    return base


class TestNormalizeExchange:
    def test_default(self) -> None:
        assert normalize_exchange(None) == "binance"
        assert normalize_exchange("") == "binance"

    def test_lower_and_strip(self) -> None:
        assert normalize_exchange("Bybit") == "bybit"
        assert normalize_exchange(" BINANCE ") == "binance"

    def test_sanitize_chars(self) -> None:
        assert normalize_exchange("okx-v5!") == "okxv5"


class TestMapEvent:
    def test_long_entry(self) -> None:
        p = _map_event_to_payload(_ev(kind="entry", direction="long", qty=0.5), "BTCUSDT")
        assert isinstance(p, dict)
        assert p["action"] == "LONG"
        assert p["quantity"] == 0.5
        assert p["exchange"] == "binance"
        assert p["symbol"] == "BTCUSDT"

    def test_short_entry(self) -> None:
        p = _map_event_to_payload(_ev(kind="entry", direction="short", qty=1.2), "ETHUSDT")
        assert isinstance(p, dict)
        assert p["action"] == "SHORT"
        assert p["quantity"] == 1.2

    def test_close_long(self) -> None:
        p = _map_event_to_payload(
            _ev(kind="close", direction="long", qty=0.25),
            "BTCUSDT",
        )
        assert isinstance(p, dict)
        assert p["action"] == "CLOSE_LONG"

    def test_close_short(self) -> None:
        p = _map_event_to_payload(
            _ev(kind="close", direction="short", qty=0.25),
            "BTCUSDT",
        )
        assert isinstance(p, dict)
        assert p["action"] == "CLOSE_SHORT"

    def test_exit_without_direction_is_close_long(self) -> None:
        p = _map_event_to_payload(_ev(kind="exit", direction=None, qty=1.0), "BTCUSDT")
        assert isinstance(p, dict)
        assert p["action"] == "CLOSE_LONG"

    def test_close_all_two_payloads(self) -> None:
        mapped = _map_event_to_payload(
            _ev(kind="close_all", direction=None, qty=1.0),
            "BTCUSDT",
        )
        assert isinstance(mapped, list)
        assert len(mapped) == 2
        actions = {p["action"] for p in mapped}
        assert actions == {"CLOSE_LONG", "CLOSE_SHORT"}
        assert all(p["quantity"] == 1.0 for p in mapped)

    def test_missing_qty_returns_none(self) -> None:
        assert _map_event_to_payload(_ev(qty=None), "BTCUSDT") is None
        assert _map_event_to_payload(_ev(qty=0), "BTCUSDT") is None
        assert _map_event_to_payload(_ev(qty=-1), "BTCUSDT") is None

    def test_custom_exchange_from_event(self) -> None:
        p = _map_event_to_payload(
            _ev(exchange="Bybit", qty=0.1),
            "BTCUSDT",
            exchange="binance",
        )
        assert isinstance(p, dict)
        assert p["exchange"] == "bybit"

    def test_custom_exchange_default_param(self) -> None:
        p = _map_event_to_payload(_ev(qty=0.1), "BTCUSDT", exchange="okx")
        assert isinstance(p, dict)
        assert p["exchange"] == "okx"

    def test_price_and_order_type_and_leverage(self) -> None:
        p = _map_event_to_payload(
            _ev(
                kind="order",
                direction="long",
                qty=0.01,
                limit=50_000.0,
                order_type="limit",
                leverage=5,
            ),
            "BTCUSDT",
        )
        assert isinstance(p, dict)
        assert p["action"] == "LONG"
        assert p["price"] == 50_000.0
        assert p["orderType"] == "limit"
        assert p["leverage"] == 5

    def test_default_leverage_param(self) -> None:
        p = _map_event_to_payload(
            _ev(qty=0.01),
            "BTCUSDT",
            default_leverage=10,
        )
        assert isinstance(p, dict)
        assert p["leverage"] == 10

    def test_cancel_skipped(self) -> None:
        assert _map_event_to_payload(_ev(kind="cancel", qty=1.0), "BTCUSDT") is None


class TestForwardEvents:
    def test_auth_header_present_when_key_provided(self) -> None:
        svc = FakeTradeService()
        meta = asyncio.run(
            forward_events(
                [_ev(qty=0.01)],
                svc,
                symbol="BTCUSDT",
                internal_auth_key="mesh-secret",
            )
        )
        assert meta["forwarded"] == 1
        assert meta["failed"] == 0
        assert len(svc.calls) == 1
        headers = svc.calls[0]["headers"]
        assert headers["X-Internal-Auth-Key"] == "mesh-secret"
        assert headers["Content-Type"] == "application/json"
        assert headers["X-Source"] == "pyne-worker"
        assert "Idempotency-Key" in headers
        assert headers["Idempotency-Key"].startswith("pyne:")

    def test_fail_when_key_missing(self) -> None:
        svc = FakeTradeService()
        meta = asyncio.run(
            forward_events(
                [_ev(qty=0.01), _ev(kind="entry", direction="short", qty=0.02)],
                svc,
                symbol="BTCUSDT",
                internal_auth_key=None,
            )
        )
        assert meta["forwarded"] == 0
        assert meta["failed"] == 2
        assert svc.calls == []
        assert any("missing internal auth key" in e for e in meta["errors"])

    def test_missing_qty_not_forwarded(self) -> None:
        svc = FakeTradeService()
        meta = asyncio.run(
            forward_events(
                [_ev(qty=None), _ev(qty=0), _ev(qty=-3)],
                svc,
                symbol="BTCUSDT",
                internal_auth_key="k",
            )
        )
        assert meta["forwarded"] == 0
        assert meta["failed"] == 3
        assert svc.calls == []
        assert all("missing qty" in e for e in meta["errors"])

    def test_close_all_forwards_two_posts(self) -> None:
        svc = FakeTradeService()
        meta = asyncio.run(
            forward_events(
                [_ev(kind="close_all", direction=None, qty=1.0)],
                svc,
                symbol="BTCUSDT",
                internal_auth_key="k",
            )
        )
        assert meta["forwarded"] == 2
        assert meta["failed"] == 0
        assert len(svc.calls) == 2
        import json

        actions = {json.loads(c["body"].decode())["action"] for c in svc.calls}
        assert actions == {"CLOSE_LONG", "CLOSE_SHORT"}
        # Distinct idempotency keys per action
        keys = {c["headers"]["Idempotency-Key"] for c in svc.calls}
        assert len(keys) == 2

    def test_custom_exchange_in_body(self) -> None:
        svc = FakeTradeService()
        meta = asyncio.run(
            forward_events(
                [_ev(qty=0.01)],
                svc,
                symbol="BTCUSDT",
                internal_auth_key="k",
                exchange="bybit",
            )
        )
        assert meta["forwarded"] == 1
        import json

        body = json.loads(svc.calls[0]["body"].decode())
        assert body["exchange"] == "bybit"
        assert body["action"] == "LONG"

    def test_event_exchange_overrides_default(self) -> None:
        svc = FakeTradeService()
        asyncio.run(
            forward_events(
                [_ev(qty=0.01, exchange="okx")],
                svc,
                symbol="BTCUSDT",
                internal_auth_key="k",
                exchange="binance",
            )
        )
        import json

        body = json.loads(svc.calls[0]["body"].decode())
        assert body["exchange"] == "okx"

    def test_trade_worker_error_status(self) -> None:
        svc = FakeTradeService(status=401)
        meta = asyncio.run(
            forward_events(
                [_ev(qty=0.01)],
                svc,
                symbol="BTCUSDT",
                internal_auth_key="k",
            )
        )
        assert meta["forwarded"] == 0
        assert meta["failed"] == 1
        assert "401" in meta["errors"][0]
