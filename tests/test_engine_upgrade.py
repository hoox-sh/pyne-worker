# Copyright (c) 2026 HOOX · PYNE · jango-blockchained
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Host upgrades for pynescript 0.6.23–0.6.30 on POST /run and Runtime."""

from __future__ import annotations

import json

from handler import handle_request
from pynescript_backend import Runtime
from security import sanitize_error_for_response
from security import to_pine_period


def _bars(n: int = 8) -> list[dict]:
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
                "time": 1_700_000_000_000 + i * 60_000,
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


class TestPinePeriod:
    def test_exchange_intervals(self) -> None:
        assert to_pine_period("1m") == "1"
        assert to_pine_period("15m") == "15"
        assert to_pine_period("1h") == "60"
        assert to_pine_period("4h") == "240"
        assert to_pine_period("1d") == "D"
        assert to_pine_period("1w") == "W"
        assert to_pine_period("1M") == "M"
        assert to_pine_period("3d") == "3D"
        assert to_pine_period("8h") == "480"

    def test_already_pine_forms(self) -> None:
        assert to_pine_period("15") == "15"
        assert to_pine_period("D") == "D"
        assert to_pine_period("30S") == "30S"
        assert to_pine_period("1D") == "D"

    def test_one_m_is_not_monthly(self) -> None:
        """``1m``.upper() is ``1M`` (monthly) — the mapper must not do that."""
        assert to_pine_period("1m") == "1"
        assert to_pine_period("1M") == "M"

    def test_unknown_is_none(self) -> None:
        assert to_pine_period("foo") is None
        assert to_pine_period("") is None
        assert to_pine_period(None) is None


class TestErrorSanitize:
    def test_drops_traceback_and_paths(self) -> None:
        blob = (
            "Traceback (most recent call last):\n"
            '  File "/home/jango/Git/pynescript/src/pynescript/runtime/host.py", line 12, in run\n'
            "RuntimeError: undeclared identifier 'foo'\n"
        )
        out = sanitize_error_for_response(blob)
        assert out == "RuntimeError: undeclared identifier 'foo'"
        assert "/home/" not in out
        assert "Traceback" not in out

    def test_redacts_secrets(self) -> None:
        out = sanitize_error_for_response("failed api_key=supersecret123 boom")
        assert "supersecret123" not in out
        assert "[redacted]" in out


class TestChartTimeframeHttp:
    async def test_15m_drives_timeframe_in_seconds(self) -> None:
        script = (
            "//@version=6\n"
            "indicator('tf')\n"
            "plot(timeframe.in_seconds(), 'sec')\n"
            "plot(timeframe.isminutes ? 1 : 0, 'minutes')\n"
            "plot(timeframe.isdaily ? 1 : 0, 'daily')\n"
        )
        body, status = await _post_run(
            {
                "script": script,
                "ohlcv": _bars(5),
                "timeframe": "15m",
                "mode": "interpret",
            }
        )
        assert status == 200, body
        assert body["series"]["sec"][-1] == 900
        assert body["series"]["minutes"][-1] == 1
        assert body["series"]["daily"][-1] == 0

    async def test_default_timeframe_is_daily(self) -> None:
        script = "//@version=6\nindicator('tf')\nplot(timeframe.in_seconds(), 'sec')\n"
        body, status = await _post_run(
            {"script": script, "ohlcv": _bars(4), "mode": "interpret"}
        )
        assert status == 200, body
        assert body["series"]["sec"][-1] == 86400


class TestTickerNew:
    def test_prefix_ticker_joins_chart_symbol(self) -> None:
        src = (
            "//@version=6\n"
            "indicator('t')\n"
            "t = ticker.new(syminfo.prefix, syminfo.ticker)\n"
            "plot(request.security(t, timeframe.period, close), 'c')\n"
        )
        # Same bar clock as pynescript's ticker.new regression lock.
        bars = [
            {
                "time": i * 60_000,
                "open": 1.0,
                "high": 1.0,
                "low": 1.0,
                "close": 1.0,
                "volume": 1.0,
            }
            for i in range(5)
        ]
        out = Runtime(symbol="NASDAQ:AAPL").run(src, bars, mode="interpret")
        assert "error" not in out, out.get("error")
        series = (out.get("series") or {}).get("c") or []
        assert series and series[-1] is not None


class TestNextBarFill:
    def test_market_entry_fills_next_bar(self) -> None:
        src = (
            "//@version=6\n"
            "strategy('nb')\n"
            "if bar_index == 1\n"
            "    strategy.entry('L', strategy.long, 1)\n"
            "plot(close)\n"
        )
        out = Runtime(symbol="TEST").run(src, _bars(6), mode="interpret")
        assert "error" not in out, out.get("error")
        events = out.get("events") or []
        entries = [
            e for e in events if isinstance(e, dict) and e.get("kind") == "entry"
        ]
        fills = [
            e
            for e in events
            if isinstance(e, dict)
            and e.get("kind") == "order"
            and e.get("comment") == "fill"
        ]
        assert entries, events
        assert entries[0].get("bar_index") == 2
        assert fills and fills[0].get("bar_index") == 2


class TestCompileParseHonesty:
    def test_garbage_is_parse_on_both_modes(self) -> None:
        bars = _bars(3)
        bad = "@@@not pine"
        inter = Runtime().run(bad, bars, mode="interpret")
        assert inter.get("error_kind") == "parse"
        comp = Runtime().run(bad, bars, mode="compile")
        assert comp.get("error_kind") in ("parse", "compile")
        assert comp.get("error")


class TestRunErrorEnvelope:
    async def test_parse_error_is_sanitized_envelope(self) -> None:
        body, status = await _post_run({"script": "@@@not pine", "ohlcv": _bars(3)})
        assert status == 500
        assert body.get("status") == "error"
        assert body.get("error_kind") == "parse"
        assert body.get("error") == body.get("message")
        assert "/home/" not in str(body.get("error"))
        assert "Traceback" not in str(body.get("error"))

    async def test_blocked_webhook_is_400(self) -> None:
        body, status = await _post_run(
            {
                "script": "//@version=5\nindicator('t')\nplot(close)",
                "ohlcv": _bars(3),
                "webhook_url": "http://127.0.0.1/hook",
            }
        )
        assert status == 400
        assert "webhook" in str(body.get("error")).lower()
        assert body.get("code") == "WEBHOOK_URL_BLOCKED"
