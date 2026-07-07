"""Smoke tests for pyne-worker bootstrap.

Verifies routing and response shapes. Full evaluator parity tests live in
pynescript/tests/test_parity.py; pyne-worker reuses the same Runtime.
"""

from __future__ import annotations

import json

from handler import handle_request


def _post(path: str, body: dict | None = None) -> tuple[dict, int]:
    raw = json.dumps(body) if body is not None else None
    payload, status = handle_request("POST", path, raw)
    return payload, status


def _get(path: str) -> tuple[dict, int]:
    return handle_request("GET", path)


class TestHealth:
    def test_returns_ok(self) -> None:
        body, status = _get("/health")
        assert status == 200
        assert body["status"] == "ok"
        assert body["worker"] == "pyne-worker"


class TestRouting:
    def test_unknown_path_404(self) -> None:
        body, status = _get("/unknown")
        assert status == 404
        assert body["error"] == "Not found"


class TestRun:
    def test_missing_script_400(self) -> None:
        body, status = _post("/run", {"ohlcv": []})
        assert status == 400
        assert body["error"] == "Missing 'script'"

    def test_no_ohlcv_returns_placeholder(self) -> None:
        body, status = _post("/run", {"script": "//@version=5\nx = 1"})
        assert status == 200
        assert body["events"] == []
        assert body["bars"] == 0
        assert "note" in body

    def test_valid_body_runs_evaluator(self) -> None:
        body, status = _post(
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

    def test_accepts_data_alias(self) -> None:
        body, status = _post(
            "/run",
            {
                "script": "//@version=5\nx = 1",
                "data": [{"open": 1, "high": 2, "low": 0.5, "close": 1.5, "time": 1}],
            },
        )
        assert status == 200
        assert body["bars"] == 1