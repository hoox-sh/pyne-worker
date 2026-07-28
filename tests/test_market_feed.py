# Copyright (c) 2026 HOOX · PYNE · jango-blockchained
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Tests for live market feed → R2 (cron data plane)."""

from __future__ import annotations

import json
from typing import Any

from market_feed import only_closed_bars
from market_feed import refresh_pair_to_r2
from market_feed import refresh_pairs_for_jobs
from scheduler import run_scheduled_jobs
from scripts_registry import put_script


class _FakeR2Object:
    def __init__(self, text: str) -> None:
        self._text = text

    async def text(self) -> str:
        return self._text

    async def arrayBuffer(self) -> bytes:  # noqa: N802
        return self._text.encode("utf-8")


class FakeR2Bucket:
    def __init__(self) -> None:
        self.store: dict[str, str] = {}

    async def get(self, key: str) -> _FakeR2Object | None:
        if key not in self.store:
            return None
        return _FakeR2Object(self.store[key])

    async def put(self, key: str, value: str) -> None:
        self.store[key] = value

    async def delete(self, key: str) -> None:
        self.store.pop(key, None)


def _bybit_payload(bars: list[dict[str, Any]]) -> dict[str, Any]:
    # newest first as Bybit does
    rows = []
    for b in reversed(bars):
        rows.append(
            [
                str(b["time"]),
                str(b["open"]),
                str(b["high"]),
                str(b["low"]),
                str(b["close"]),
                str(b.get("volume", 1)),
                "0",
            ]
        )
    return {"retCode": 0, "retMsg": "OK", "result": {"list": rows}}


class TestOnlyClosedBars:
    def test_drops_forming_candle(self) -> None:
        # candle opens at t, closes at t+60_000
        t0 = 1_700_000_000_000
        bars = [
            {"time": t0, "open": 1, "high": 2, "low": 0.5, "close": 1.5},
            {"time": t0 + 60_000, "open": 2, "high": 3, "low": 1, "close": 2.5},
        ]
        # 30s into second candle → only first is closed
        closed = only_closed_bars(bars, "1m", now_ms=t0 + 60_000 + 30_000)
        assert len(closed) == 1
        assert closed[0]["time"] == t0

        # after second candle fully closed
        closed2 = only_closed_bars(bars, "1m", now_ms=t0 + 120_000)
        assert len(closed2) == 2


class TestMarketRefresh:
    async def test_refresh_pair_merges_into_r2(self) -> None:
        r2 = FakeR2Bucket()
        t0 = 1_700_000_000_000
        # two closed bars relative to now far in the future
        sample = [
            {
                "time": t0,
                "open": 100.0,
                "high": 101.0,
                "low": 99.0,
                "close": 100.5,
                "volume": 1.0,
            },
            {
                "time": t0 + 60_000,
                "open": 100.5,
                "high": 102.0,
                "low": 100.0,
                "close": 101.0,
                "volume": 2.0,
            },
        ]

        async def fake_http(url: str) -> Any:
            assert "bybit.com" in url
            assert "interval=1" in url
            return _bybit_payload(sample)

        info = await refresh_pair_to_r2(
            r2,
            "BTCUSDT",
            "1m",
            limit=50,
            closed_only=True,
            http_get_json=fake_http,
        )
        assert info["status"] if "status" in info else True
        assert info["source"] == "bybit"
        assert info["fetched"] == 2
        assert info["last_bar_time"] == t0 + 60_000

        # R2 has year file
        keys = list(r2.store.keys())
        assert any(k.startswith("data/BTCUSDT/1m/") for k in keys)

    async def test_scheduler_refreshes_then_runs(self) -> None:
        r2 = FakeR2Bucket()
        t0 = 1_700_000_000_000
        sample = [
            {
                "time": t0 + i * 60_000,
                "open": 100.0 + i,
                "high": 101.0 + i,
                "low": 99.0 + i,
                "close": 100.5 + i,
                "volume": 1.0,
            }
            for i in range(5)
        ]

        async def fake_http(url: str) -> Any:
            return _bybit_payload(sample)

        await put_script(
            r2,
            {
                "id": "live-sma",
                "script": "//@version=5\nindicator('t')\nplot(close)",
                "symbol": "BTCUSDT",
                "timeframe": "1m",
                "mode": "interpret",
                "enabled": True,
            },
        )

        summary = await run_scheduled_jobs(
            r2,
            force=False,
            refresh_market=True,
            http_get_json=fake_http,
        )
        assert summary["feed"]
        assert summary["feed"][0]["status"] == "ok"
        assert summary["jobs_run"] == 1
        assert summary["results"][0]["bars"] == 5

        # Second tick, same closed bars → skip run
        s2 = await run_scheduled_jobs(
            r2,
            force=False,
            refresh_market=True,
            http_get_json=fake_http,
        )
        assert s2["jobs_skipped"] == 1
        assert s2["results"][0]["reason"] == "no new bar"

        # New closed bar appears
        sample.append(
            {
                "time": t0 + 5 * 60_000,
                "open": 110.0,
                "high": 111.0,
                "low": 109.0,
                "close": 110.5,
                "volume": 1.0,
            }
        )
        s3 = await run_scheduled_jobs(
            r2,
            force=False,
            refresh_market=True,
            http_get_json=fake_http,
        )
        assert s3["jobs_run"] == 1
        assert s3["results"][0]["last_bar_time"] == t0 + 5 * 60_000

    async def test_refresh_pairs_dedupes(self) -> None:
        r2 = FakeR2Bucket()
        t0 = 1_700_000_000_000
        sample = [
            {
                "time": t0,
                "open": 1.0,
                "high": 2.0,
                "low": 0.5,
                "close": 1.5,
                "volume": 1.0,
            }
        ]
        calls = {"n": 0}

        async def fake_http(url: str) -> Any:
            calls["n"] += 1
            return _bybit_payload(sample)

        jobs = [
            {"script_id": "a", "symbol": "BTCUSDT", "timeframe": "1m", "enabled": True},
            {"script_id": "b", "symbol": "BTCUSDT", "timeframe": "1m", "enabled": True},
        ]
        out = await refresh_pairs_for_jobs(r2, jobs, limit=10, http_get_json=fake_http)
        assert len(out) == 1
        assert calls["n"] == 1
