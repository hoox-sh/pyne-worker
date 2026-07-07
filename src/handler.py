"""HTTP routing for pyne-worker — testable without the Workers runtime."""

from __future__ import annotations

import json
from typing import Any

from pynescript_backend import Runtime


def _json_response(body: dict[str, Any], status: int = 200) -> tuple[dict[str, Any], int]:
    return body, status


def _parse_body(raw: str | None) -> tuple[dict[str, Any] | None, tuple[dict[str, Any], int] | None]:
    if not raw:
        return None, _json_response({"error": "Missing request body"}, 400)

    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return None, _json_response({"error": "Invalid JSON"}, 400)

    if not isinstance(data, dict):
        return None, _json_response({"error": "Request body must be a JSON object"}, 400)

    return data, None


def _normalize_ohlcv(body: dict[str, Any]) -> list[dict[str, Any]] | None:
    """Accept ``ohlcv`` (pine-worker) or ``data`` (pynescript Pro API)."""
    bars = body.get("ohlcv")
    if bars is None:
        bars = body.get("data")
    if bars is None:
        return None
    if not isinstance(bars, list):
        return None
    return bars


def handle_request(method: str, path: str, body: str | None = None) -> tuple[dict[str, Any], int]:
    """Route a request and return ``(response_dict, status_code)``."""
    if path == "/health" and method == "GET":
        return _json_response({"status": "ok", "worker": "pyne-worker"})

    if path == "/run" and method == "POST":
        return handle_run(body)

    return _json_response({"error": "Not found"}, 404)


def handle_run(body: str | None) -> tuple[dict[str, Any], int]:
    data, err = _parse_body(body)
    if err is not None:
        return err

    assert data is not None
    script = data.get("script")
    if not script or not isinstance(script, str):
        return _json_response({"error": "Missing 'script'"}, 400)

    ohlcv = _normalize_ohlcv(data)
    if ohlcv is None:
        return _json_response(
            {
                "events": [],
                "bars": 0,
                "note": "Evaluator ready — provide 'ohlcv' or 'data' to run",
            }
        )

    if len(ohlcv) == 0:
        return _json_response({"error": "OHLCV data must not be empty"}, 400)

    symbol = data.get("symbol", "BTCUSDT")
    if not isinstance(symbol, str):
        symbol = "BTCUSDT"

    runtime = Runtime(symbol=symbol)
    result = runtime.run(script, ohlcv)

    if "error" in result:
        return _json_response({"error": result["error"]}, 500)

    return _json_response(
        {
            "events": result.get("events", []),
            "plots": result.get("plots", []),
            "bars": result.get("count", len(ohlcv)),
            "script_id": result.get("script_id", ""),
            "run_id": result.get("run_id", ""),
        }
    )