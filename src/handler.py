# Copyright (c) 2026 HOOX · PYNE · jango-blockchained
# SPDX-License-Identifier: AGPL-3.0-or-later

"""HTTP routing for pyne-worker — testable without the Workers runtime.

This module implements the request pipeline with middleware integration
for auth, rate limiting, input validation, and structured logging.
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

from middleware import LogHelper
from middleware import RateLimiter
from middleware import validate_api_key
from pynescript_backend import Runtime

# ---------------------------------------------------------------------------
# Limits
# ---------------------------------------------------------------------------

_MAX_SCRIPT_LENGTH = 100_000  # characters
_MAX_BARS = 100_000
_MAX_PAYLOAD_BYTES = 5 * 1024 * 1024  # 5 MB
_REQUIRED_BAR_FIELDS = {"open", "high", "low", "close", "time"}

# ---------------------------------------------------------------------------
# Singleton middleware instances
# ---------------------------------------------------------------------------

_rate_limiter = RateLimiter()


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _json_response(
    body: dict[str, Any],
    status: int = 200,
    headers: dict[str, str] | None = None,
) -> tuple[dict[str, Any], int, dict[str, str]]:
    return body, status, headers or {}


def _parse_body(raw: str | None) -> tuple[dict[str, Any] | None, tuple[dict[str, Any], int, dict[str, str]] | None]:
    if not raw:
        return None, _json_response({"error": "Missing request body"}, 400)

    if len(raw) > _MAX_PAYLOAD_BYTES:
        return None, _json_response({"error": "Request body too large"}, 413)

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


def _validate_ohlcv(ohlcv: list[dict[str, Any]]) -> str | None:
    """Validate OHLCV bar data.

    Returns ``None`` if valid, or an error message string.
    """
    if not ohlcv:
        return "OHLCV data must not be empty"
    if len(ohlcv) > _MAX_BARS:
        return f"OHLCV data exceeds {_MAX_BARS} bars"
    for i, bar in enumerate(ohlcv):
        if not isinstance(bar, dict):
            return f"Bar at index {i} is not a dict"
        missing = _REQUIRED_BAR_FIELDS - set(bar)
        if missing:
            return f"Bar at index {i} missing fields: {', '.join(sorted(missing))}"
        for field in ("open", "high", "low", "close"):
            val = bar.get(field)
            if not isinstance(val, (int, float)):
                return f"Bar at index {i} '{field}' is not a number"
    return None


# ---------------------------------------------------------------------------
# Request pipeline
# ---------------------------------------------------------------------------


async def handle_request(
    method: str,
    path: str,
    body: str | None = None,
    r2_bucket: Any = None,
    api_key: str | None = None,
    expected_api_key: str | None = None,
    request_id: str | None = None,
) -> tuple[dict[str, Any], int, dict[str, str]]:
    """Route and process a request through the middleware pipeline.

    Args:
        method: HTTP method.
        path: Request path.
        body: Raw request body text.
        r2_bucket: Optional R2 bucket binding.
        api_key: ``X-API-Key`` header value (or ``None``).
        expected_api_key: The expected secret, or ``None`` to disable auth.
        request_id: Unique request identifier for logging.

    Returns:
        ``(response_dict, status_code, response_headers)``.
    """
    # -- 404 for unknown routes (no middleware needed) --------------------
    if path not in ("/health", "/run", "/ingest"):
        return _json_response({"error": "Not found"}, 404)

    # -- Health endpoint — enhanced with dependency checks ----------------
    if path == "/health" and method == "GET":
        return _handle_health(r2_bucket)

    # -- Auth -------------------------------------------------------------
    if not validate_api_key(api_key, expected_api_key):
        return _json_response({"error": "Unauthorized"}, 401)

    # -- Rate limit -------------------------------------------------------
    rate_key = api_key or "anonymous"
    allowed, rate_headers = _rate_limiter.check(rate_key)
    if not allowed:
        return _json_response(
            {"error": "Rate limit exceeded"},
            429,
            headers=rate_headers,
        )

    # -- Route ------------------------------------------------------------
    if path == "/run" and method == "POST":
        resp_body, resp_status, resp_headers = await handle_run(body, r2_bucket=r2_bucket)
        resp_headers.update(rate_headers)
        return resp_body, resp_status, resp_headers

    if path == "/ingest" and method == "POST":
        resp_body, resp_status, resp_headers = await handle_ingest(body, r2_bucket=r2_bucket)
        resp_headers.update(rate_headers)
        return resp_body, resp_status, resp_headers

    return _json_response({"error": "Method not allowed"}, 405)


# ---------------------------------------------------------------------------
# /health
# ---------------------------------------------------------------------------


async def _check_deps_async(r2_bucket: Any | None) -> dict[str, str]:
    """Asynchronously check dependency health.

    Returns a dict of ``{dependency_name: "ok" | "error: ..."}``.
    """
    status: dict[str, str] = {}
    # R2 check
    if r2_bucket is not None:
        try:
            await r2_bucket.head("__health_check__")
            status["r2"] = "ok"
        except Exception as e:
            status["r2"] = f"error: {e!s}"
    else:
        status["r2"] = "not_configured"
    return status


def _handle_health(r2_bucket: Any | None) -> tuple[dict[str, Any], int, dict[str, str]]:
    """Health endpoint — returns status and dependency checks.

    Synchronously checks in-memory state; async checks are done separately
    in entry.py when the response is built.
    """
    return _json_response(
        {
            "status": "ok",
            "worker": "pyne-worker",
            "version": "0.4.0",
        }
    )


# ---------------------------------------------------------------------------
# POST /run
# ---------------------------------------------------------------------------


async def handle_run(
    body: str | None,
    r2_bucket: Any = None,
) -> tuple[dict[str, Any], int, dict[str, str]]:
    """Execute a Pine Script strategy over OHLCV data."""
    data, err = _parse_body(body)
    if err is not None:
        return err

    if data is None:
        return _json_response({"error": "Failed to parse request body"}, 400)

    script = data.get("script")
    if not script or not isinstance(script, str):
        return _json_response({"error": "Missing or invalid 'script'"}, 400)

    if len(script) > _MAX_SCRIPT_LENGTH:
        return _json_response(
            {"error": f"Script exceeds {_MAX_SCRIPT_LENGTH} character limit"},
            413,
        )

    ohlcv = _normalize_ohlcv(data)
    symbol: str = data.get("symbol", "BTCUSDT")
    timeframe: str = data.get("timeframe", "1d")
    if not isinstance(symbol, str):
        symbol = "BTCUSDT"
    if not isinstance(timeframe, str):
        timeframe = "1d"

    # Only auto-fetch from R2/Binance when both symbol+timeframe are
    # explicitly provided (not just defaults).
    has_explicit_symbol = "symbol" in data and isinstance(data["symbol"], str)
    has_explicit_timeframe = "timeframe" in data and isinstance(data["timeframe"], str)

    if ohlcv is None and (has_explicit_symbol or has_explicit_timeframe):
        # Try R2 first
        if r2_bucket is not None:
            try:
                from data_provider import fetch_ohlcv_from_r2

                ohlcv = await fetch_ohlcv_from_r2(r2_bucket, symbol, timeframe)
            except Exception as e:
                return _json_response({"error": f"Failed to read R2: {e!s}"}, 502)
        else:
            return _json_response(
                {
                    "error": "R2 bucket not configured — preload data or provide inline 'ohlcv'/'data'",
                },
                501,
            )

    if not ohlcv:
        return _json_response(
            {
                "events": [],
                "bars": 0,
                "note": "No data found for this symbol/timeframe. "
                "Preload via POST /ingest, or provide 'ohlcv'/'data' inline.",
            }
        )

    # Validate OHLCV
    validation_err = _validate_ohlcv(ohlcv)
    if validation_err:
        return _json_response({"error": validation_err}, 400)

    # Execute with timeout via Runtime
    runtime = Runtime(symbol=symbol)
    result = runtime.run(script, ohlcv, timeout_seconds=30.0)

    if "error" in result:
        status = 504 if "timed out" in result.get("error", "").lower() else 500
        return _json_response({"error": result["error"]}, status)

    if result.get("timed_out"):
        return _json_response({"error": "Script execution timed out"}, 504)

    return _json_response(
        {
            "events": result.get("events", []),
            "plots": result.get("plots", []),
            "bars": result.get("count", len(ohlcv)),
            "script_id": result.get("script_id", ""),
            "run_id": result.get("run_id", ""),
        }
    )


# ---------------------------------------------------------------------------
# POST /ingest — R2 data ingestion
# ---------------------------------------------------------------------------


async def handle_ingest(
    body: str | None,
    r2_bucket: Any = None,
) -> tuple[dict[str, Any], int, dict[str, str]]:
    """Ingest OHLCV data into the R2 bucket.

    Expects a JSON body with:
    - ``symbol`` (str) — trading pair
    - ``timeframe`` (str) — interval, e.g. ``"1d"``
    - ``bars`` (list of dict) — bar data to append
    """
    if r2_bucket is None:
        return _json_response({"error": "R2 bucket not configured"}, 501)

    data, err = _parse_body(body)
    if err is not None:
        return err

    if data is None:
        return _json_response({"error": "Failed to parse request body"}, 400)

    symbol = data.get("symbol")
    if not symbol or not isinstance(symbol, str):
        return _json_response({"error": "Missing or invalid 'symbol'"}, 400)

    timeframe = data.get("timeframe")
    if not timeframe or not isinstance(timeframe, str):
        return _json_response({"error": "Missing or invalid 'timeframe'"}, 400)

    bars = data.get("bars")
    if not bars or not isinstance(bars, list):
        return _json_response({"error": "Missing or invalid 'bars'"}, 400)

    validation_err = _validate_ohlcv(bars)
    if validation_err:
        return _json_response({"error": f"Invalid bars: {validation_err}"}, 400)

    try:
        from data_provider import ingest_ohlcv_to_r2

        ingested = await ingest_ohlcv_to_r2(r2_bucket, symbol, timeframe, bars)
    except Exception as e:
        return _json_response({"error": f"Failed to ingest data: {e!s}"}, 500)

    return _json_response(
        {
            "ingested": ingested,
            "symbol": symbol.upper(),
            "timeframe": timeframe,
        }
    )
