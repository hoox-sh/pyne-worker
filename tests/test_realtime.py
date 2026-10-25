# Copyright (c) 2026 HOOX · PYNE · jango-blockchained
# SPDX-License-Identifier: AGPL-3.0-or-later

"""POST /run realtime_* fields (pynescript 0.6.14+ Runtime.run kwargs)."""

from __future__ import annotations

import json

from handler import handle_request


_RT_SCRIPT = """
//@version=5
indicator("rt")
var int v = 0
varip int vip = 0
v := v + 1
vip := vip + 1
plot(v, "v")
plot(vip, "vip")
"""


def _bars(n: int = 12) -> list[dict]:
    out: list[dict] = []
    price = 100.0
    for i in range(n):
        o = price
        c = price + (1.0 if i % 2 == 0 else -0.5)
        out.append(
            {
                "open": o,
                "high": max(o, c) + 0.5,
                "low": min(o, c) - 0.5,
                "close": c,
                "time": 1_700_000_000 + i * 86_400,
                "volume": 1000.0,
            }
        )
        price = c
    return out


async def _post_run(body: dict) -> tuple[dict, int]:
    payload, status, _headers = await handle_request(
        "POST",
        "/run",
        json.dumps(body),
        api_key="test-key-123",
        expected_api_key="test-key-123",
    )
    return payload, status


class TestRunRealtime:
    async def test_default_is_historical(self) -> None:
        body, status = await _post_run({"script": _RT_SCRIPT, "ohlcv": _bars(12)})
        assert status == 200
        assert body["series"]["v"][-1] == 12
        assert body["series"]["vip"][-1] == 12

    async def test_realtime_last_bar_reinit_varip(self) -> None:
        body, status = await _post_run(
            {
                "script": _RT_SCRIPT,
                "ohlcv": _bars(12),
                "realtime_last_bar": True,
            }
        )
        assert status == 200, body
        assert body["series"]["v"][-1] == 12
        assert body["series"]["vip"][-1] == 1

    async def test_realtime_ticks_enables_last_bar(self) -> None:
        body, status = await _post_run(
            {
                "script": _RT_SCRIPT,
                "ohlcv": _bars(12),
                "realtime_ticks": 2,
            }
        )
        assert status == 200, body
        assert body["series"]["vip"][-1] == 1

    async def test_realtime_last_bar_rejects_non_bool(self) -> None:
        body, status = await _post_run(
            {
                "script": _RT_SCRIPT,
                "ohlcv": _bars(3),
                "realtime_last_bar": 1,
            }
        )
        assert status == 400
        assert "realtime_last_bar" in body.get("error", "")

    async def test_realtime_ticks_rejects_bool_and_zero(self) -> None:
        body, status = await _post_run(
            {
                "script": _RT_SCRIPT,
                "ohlcv": _bars(3),
                "realtime_ticks": True,
            }
        )
        assert status == 400
        assert "realtime_ticks" in body.get("error", "")
        body, status = await _post_run(
            {
                "script": _RT_SCRIPT,
                "ohlcv": _bars(3),
                "realtime_ticks": 0,
            }
        )
        assert status == 400
        assert "realtime_ticks" in body.get("error", "")

    async def test_health_advertises_realtime(self) -> None:
        payload, status, _ = await handle_request("GET", "/health")
        assert status == 200
        assert payload["features"].get("realtime") is True
        assert payload["version"] == "0.8.0"
        assert str(payload.get("engine", "")).startswith("0.6.22")
