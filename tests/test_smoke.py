# Copyright (c) 2026 HOOX · PYNE · jango-blockchained
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Smoke tests for pyne-worker bootstrap.

Verifies routing and response shapes. Full evaluator parity tests live in
pynescript/tests/test_parity.py; pyne-worker reuses the same Runtime.
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

import asyncio
import json

from handler import handle_request


async def _post(
    path: str,
    body: dict | None = None,
    api_key: str | None = "test-key-123",
) -> tuple[dict, int]:
    raw = json.dumps(body) if body is not None else None
    payload, status, _headers = await handle_request(
        "POST",
        path,
        raw,
        api_key=api_key,
        expected_api_key="test-key-123",
    )
    return payload, status


async def _get(path: str) -> tuple[dict, int]:
    payload, status, _headers = await handle_request("GET", path)
    return payload, status


class TestHealth:
    async def test_returns_ok(self) -> None:
        body, status = await _get("/health")
        assert status == 200
        assert body["status"] == "ok"
        assert body["worker"] == "pyne-worker"


class TestRouting:
    async def test_unknown_path_404(self) -> None:
        body, status = await _get("/unknown")
        assert status == 404
        assert body["error"] == "Not found"

    async def test_health_without_auth(self) -> None:
        """Health endpoint should not require auth."""
        payload, status, _headers = await handle_request("GET", "/health")
        assert status == 200
        assert payload["status"] == "ok"


class TestAuth:
    async def test_missing_key_returns_401(self) -> None:
        payload, status, _headers = await handle_request(
            "POST",
            "/run",
            '{"script":"test"}',
            api_key=None,
            expected_api_key="secret-123",
        )
        assert status == 401
        assert "Unauthorized" in payload.get("error", "")

    async def test_wrong_key_returns_401(self) -> None:
        payload, status, _headers = await handle_request(
            "POST",
            "/run",
            '{"script":"test"}',
            api_key="wrong-key",
            expected_api_key="secret-123",
        )
        assert status == 401
        assert "Unauthorized" in payload.get("error", "")

    async def test_dev_mode_no_key_allowed(self) -> None:
        """When expected_api_key is None/empty, all requests pass."""
        payload, status, _headers = await handle_request(
            "POST",
            "/run",
            '{"script":"//@version=5\\nx=1","data":[{"open":1,"high":2,"low":0.5,"close":1.5,"time":1}]}',
            api_key=None,
            expected_api_key=None,
        )
        assert status == 200


class TestRun:
    async def test_missing_script_400(self) -> None:
        body, status = await _post("/run", {"ohlcv": []})
        assert status == 400
        assert "script" in body.get("error", "").lower()

    async def test_script_too_long(self) -> None:
        body, status = await _post(
            "/run",
            {"script": "x" * 100_001, "data": [{"open": 1, "high": 2, "low": 0.5, "close": 1.5, "time": 1}]},
        )
        assert status == 413

    async def test_no_ohlcv_returns_placeholder(self) -> None:
        body, status = await _post("/run", {"script": "//@version=5\nx = 1"})
        assert status == 200
        assert body["events"] == []
        assert body["bars"] == 0
        assert "note" in body

    async def test_valid_body_runs_evaluator(self) -> None:
        body, status = await _post(
            "/run",
            {
                "script": "//@version=5\nindicator('test')\nplot(close)",
                "ohlcv": [
                    {"open": 100, "high": 105, "low": 95, "close": 102, "time": 1000},
                    {"open": 102, "high": 108, "low": 101, "close": 106, "time": 2000},
                ],
            },
        )
        assert status == 200
        assert body["bars"] == 2
        assert "events" in body
        assert "script_id" in body
        assert "run_id" in body

    async def test_accepts_data_alias(self) -> None:
        body, status = await _post(
            "/run",
            {
                "script": "//@version=5\nx = 1",
                "data": [{"open": 1, "high": 2, "low": 0.5, "close": 1.5, "time": 1}],
            },
        )
        assert status == 200
        assert body["bars"] == 1

    async def test_empty_ohlcv_returns_note(self) -> None:
        body, status = await _post(
            "/run",
            {"script": "//@version=5\nx = 1", "data": []},
        )
        assert status == 200
        assert "note" in body

    async def test_malformed_ohlcv_400(self) -> None:
        """A bar missing required fields should be rejected."""
        body, status = await _post(
            "/run",
            {
                "script": "//@version=5\nx = 1",
                "data": [{"open": 1, "high": 2, "close": 1.5}],  # missing low, time
            },
        )
        assert status == 400
        assert "missing" in body.get("error", "").lower()


class TestRateLimit:
    async def test_rate_limit_headers_present(self) -> None:
        payload, status, headers = await handle_request(
            "POST",
            "/run",
            '{"script":"//@version=5\\nx=1","data":[{"open":1,"high":2,"low":0.5,"close":1.5,"time":1}]}',
            api_key="test-key-123",
            expected_api_key="test-key-123",
        )
        assert status == 200
        assert "X-RateLimit-Limit" in headers
        assert "X-RateLimit-Remaining" in headers


class TestIngest:
    async def test_ingest_without_r2_returns_501(self) -> None:
        payload, status, _headers = await handle_request(
            "POST",
            "/ingest",
            json.dumps({"symbol": "BTCUSDT", "timeframe": "1d", "bars": []}),
            api_key="test-key-123",
            expected_api_key="test-key-123",
            r2_bucket=None,
        )
        assert status == 501
        assert "not configured" in payload.get("error", "").lower()
