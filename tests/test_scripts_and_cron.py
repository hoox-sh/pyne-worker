# Copyright (c) 2026 HOOX · PYNE · jango-blockchained
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Tests for mode= on /run, script registry, and bar-close scheduler."""

from __future__ import annotations

import json
from typing import Any

from handler import handle_request
from scheduler import run_scheduled_jobs


class _FakeR2Object:
    def __init__(self, text: str) -> None:
        self._text = text

    async def text(self) -> str:
        return self._text

    async def arrayBuffer(self) -> bytes:  # noqa: N802 — match R2 API shape
        return self._text.encode("utf-8")


class FakeR2Bucket:
    """Minimal async R2 bucket for unit tests."""

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

    async def head(self, key: str) -> _FakeR2Object | None:
        return await self.get(key)


def _bars(n: int = 5, start_time: int = 1_700_000_000_000, step_ms: int = 60_000) -> list[dict[str, Any]]:
    out = []
    for i in range(n):
        c = 100.0 + i
        out.append(
            {
                "open": c,
                "high": c + 1,
                "low": c - 1,
                "close": c + 0.5,
                "time": start_time + i * step_ms,
                "volume": 10.0,
            }
        )
    return out


async def _api(
    method: str,
    path: str,
    body: dict | None = None,
    *,
    r2: FakeR2Bucket | None = None,
) -> tuple[dict, int]:
    raw = json.dumps(body) if body is not None else None
    payload, status, _ = await handle_request(
        method,
        path,
        raw,
        r2_bucket=r2,
        api_key="test-key-123",
        expected_api_key="test-key-123",
    )
    return payload, status


SIMPLE_PINE = "//@version=5\nindicator('t')\nplot(close)"


class TestRunMode:
    async def test_mode_interpret_echoed(self) -> None:
        body, status = await _api(
            "POST",
            "/run",
            {
                "script": SIMPLE_PINE,
                "ohlcv": _bars(3),
                "mode": "interpret",
            },
        )
        assert status == 200
        assert body.get("mode") == "interpret"
        assert body["bars"] == 3

    async def test_mode_auto_runs(self) -> None:
        body, status = await _api(
            "POST",
            "/run",
            {
                "script": SIMPLE_PINE,
                "ohlcv": _bars(4),
                "mode": "auto",
            },
        )
        assert status == 200
        assert body["bars"] == 4
        # auto may be compile or interpret depending on numba
        assert body.get("mode") in ("compile", "interpret", "auto") or body.get("auto_backend") in (
            "compile",
            "interpret",
            None,
        )

    async def test_invalid_mode_400(self) -> None:
        body, status = await _api(
            "POST",
            "/run",
            {"script": SIMPLE_PINE, "ohlcv": _bars(2), "mode": "turbo"},
        )
        assert status == 400
        assert "mode" in body.get("error", "").lower()

    async def test_mode_compile_or_error(self) -> None:
        """compile mode either succeeds or returns a clear error (no Numba on some envs)."""
        body, status = await _api(
            "POST",
            "/run",
            {
                "script": SIMPLE_PINE,
                "ohlcv": _bars(8),
                "mode": "compile",
            },
        )
        assert status in (200, 500)
        if status == 200:
            assert body.get("mode") == "compile"
            assert body["bars"] == 8
        else:
            assert "error" in body


class TestScriptRegistry:
    async def test_deploy_list_get_run(self) -> None:
        r2 = FakeR2Bucket()
        # seed 1m bars
        from data_provider import ingest_ohlcv_to_r2

        await ingest_ohlcv_to_r2(r2, "BTCUSDT", "1m", _bars(10))

        put_body, put_status = await _api(
            "POST",
            "/scripts",
            {
                "id": "sma-bot",
                "name": "SMA bot",
                "script": SIMPLE_PINE,
                "symbol": "BTCUSDT",
                "timeframe": "1m",
                "mode": "auto",
                "enabled": True,
                "max_bars": 500,
            },
            r2=r2,
        )
        assert put_status == 200, put_body
        assert put_body["script"]["id"] == "sma-bot"
        assert put_body["script"]["timeframe"] == "1m"

        listed, list_status = await _api("GET", "/scripts", r2=r2)
        assert list_status == 200
        assert listed["count"] == 1
        assert listed["scripts"][0]["id"] == "sma-bot"
        assert "script" not in listed["scripts"][0]  # list omits source

        got, get_status = await _api("GET", "/scripts/sma-bot", r2=r2)
        assert get_status == 200
        assert "plot(close)" in got["script"]["script"]

        run_body, run_status = await _api(
            "POST",
            "/run",
            {"script_id": "sma-bot"},
            r2=r2,
        )
        assert run_status == 200, run_body
        assert run_body["bars"] == 10
        assert run_body.get("deployed_script_id") == "sma-bot"
        assert run_body.get("timeframe") == "1m"

    async def test_delete_script(self) -> None:
        r2 = FakeR2Bucket()
        await _api(
            "POST",
            "/scripts",
            {"id": "tmp", "script": SIMPLE_PINE, "timeframe": "1m"},
            r2=r2,
        )
        del_body, del_status = await _api("DELETE", "/scripts/tmp", r2=r2)
        assert del_status == 200
        assert del_body["deleted"] == "tmp"
        _, get_status = await _api("GET", "/scripts/tmp", r2=r2)
        assert get_status == 404

    async def test_invalid_script_id(self) -> None:
        r2 = FakeR2Bucket()
        body, status = await _api(
            "POST",
            "/scripts",
            {"id": "../evil", "script": SIMPLE_PINE},
            r2=r2,
        )
        assert status == 400


class TestCronScheduler:
    async def test_bar_close_skips_then_runs_on_new_bar(self) -> None:
        r2 = FakeR2Bucket()
        from data_provider import ingest_ohlcv_to_r2

        bars = _bars(5, start_time=1_700_000_000_000)
        await ingest_ohlcv_to_r2(r2, "BTCUSDT", "1m", bars)

        await _api(
            "POST",
            "/scripts",
            {
                "id": "cron-sma",
                "script": SIMPLE_PINE,
                "symbol": "BTCUSDT",
                "timeframe": "1m",
                "mode": "interpret",
                "enabled": True,
            },
            r2=r2,
        )

        # First run — should execute (no live feed in unit test)
        s1 = await run_scheduled_jobs(r2, force=False, refresh_market=False)
        assert s1["jobs_run"] == 1
        assert s1["results"][0]["status"] == "ok"
        last_t = bars[-1]["time"]
        assert s1["results"][0]["last_bar_time"] == last_t

        # Second run — same bar → skip
        s2 = await run_scheduled_jobs(r2, force=False, refresh_market=False)
        assert s2["jobs_skipped"] == 1
        assert s2["results"][0]["reason"] == "no new bar"

        # Ingest a new closed bar
        new_bar = {
            "open": 200.0,
            "high": 201.0,
            "low": 199.0,
            "close": 200.5,
            "time": last_t + 60_000,
            "volume": 5.0,
        }
        await ingest_ohlcv_to_r2(r2, "BTCUSDT", "1m", [new_bar])

        s3 = await run_scheduled_jobs(r2, force=False, refresh_market=False)
        assert s3["jobs_run"] == 1
        assert s3["results"][0]["last_bar_time"] == new_bar["time"]

    async def test_cron_run_endpoint(self) -> None:
        r2 = FakeR2Bucket()
        from data_provider import ingest_ohlcv_to_r2

        await ingest_ohlcv_to_r2(r2, "BTCUSDT", "1m", _bars(3))
        await _api(
            "POST",
            "/scripts",
            {
                "id": "ep",
                "script": SIMPLE_PINE,
                "symbol": "BTCUSDT",
                "timeframe": "1m",
                "mode": "interpret",
            },
            r2=r2,
        )
        # Skip live feed in unit test (use preloaded R2 bars)
        body, status = await _api(
            "POST",
            "/cron/run",
            {"force": True, "refresh_market": False},
            r2=r2,
        )
        assert status == 200, body
        assert body["jobs_run"] == 1

    async def test_put_cron_jobs(self) -> None:
        r2 = FakeR2Bucket()
        await _api(
            "POST",
            "/scripts",
            {"id": "a", "script": SIMPLE_PINE, "timeframe": "1m"},
            r2=r2,
        )
        body, status = await _api(
            "PUT",
            "/cron/jobs",
            {
                "jobs": [
                    {
                        "script_id": "a",
                        "symbol": "BTCUSDT",
                        "timeframe": "1m",
                        "mode": "auto",
                        "enabled": True,
                    }
                ]
            },
            r2=r2,
        )
        assert status == 200
        assert body["count"] == 1
        got, gstatus = await _api("GET", "/cron/jobs", r2=r2)
        assert gstatus == 200
        assert got["jobs"][0]["script_id"] == "a"


class TestHealthFeatures:
    async def test_health_lists_features(self) -> None:
        payload, status, _ = await handle_request("GET", "/health")
        assert status == 200
        assert payload["version"] == "0.5.0"
        assert "modes" in payload.get("features", {})
