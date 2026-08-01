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
# Path helpers
# ---------------------------------------------------------------------------

_KNOWN_ROOTS = frozenset({"/health", "/run", "/ingest", "/scripts", "/cron", "/schedules", "/feed"})


def _match_route(path: str) -> tuple[str, str | None]:
    """Return ``(route_key, param)`` for known paths.

    Examples:
      /scripts          → ("/scripts", None)
      /scripts/my-id    → ("/scripts/:id", "my-id")
      /cron/jobs        → ("/cron/jobs", None)
      /cron/run         → ("/cron/run", None)
      /feed/refresh     → ("/feed/refresh", None)
    """
    if path in ("/cron/jobs", "/cron/run", "/feed/refresh"):
        return path, None
    if path == "/schedules":
        return "/cron/jobs", None
    if path in _KNOWN_ROOTS:
        return path, None
    if path.startswith("/scripts/"):
        sid = path[len("/scripts/") :].strip("/")
        if sid and "/" not in sid:
            return "/scripts/:id", sid
    return path, None


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
    route, param = _match_route(path)

    # -- Health endpoint — enhanced with dependency checks ----------------
    if route == "/health" and method == "GET":
        return _handle_health(r2_bucket)

    # -- 404 for unknown routes (no middleware needed) --------------------
    known = {
        "/run",
        "/ingest",
        "/scripts",
        "/scripts/:id",
        "/cron/jobs",
        "/cron/run",
        "/feed/refresh",
    }
    if route not in known and route != "/health":
        return _json_response({"error": "Not found"}, 404)

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
    if route == "/run" and method == "POST":
        resp_body, resp_status, resp_headers = await handle_run(body, r2_bucket=r2_bucket)
        resp_headers.update(rate_headers)
        return resp_body, resp_status, resp_headers

    if route == "/ingest" and method == "POST":
        resp_body, resp_status, resp_headers = await handle_ingest(body, r2_bucket=r2_bucket)
        resp_headers.update(rate_headers)
        return resp_body, resp_status, resp_headers

    if route == "/scripts" and method == "GET":
        resp_body, resp_status, resp_headers = await handle_list_scripts(r2_bucket=r2_bucket)
        resp_headers.update(rate_headers)
        return resp_body, resp_status, resp_headers

    if route == "/scripts" and method == "POST":
        resp_body, resp_status, resp_headers = await handle_put_script(body, r2_bucket=r2_bucket)
        resp_headers.update(rate_headers)
        return resp_body, resp_status, resp_headers

    if route == "/scripts/:id" and method == "GET" and param is not None:
        resp_body, resp_status, resp_headers = await handle_get_script(param, r2_bucket=r2_bucket)
        resp_headers.update(rate_headers)
        return resp_body, resp_status, resp_headers

    if route == "/scripts/:id" and method == "DELETE" and param is not None:
        resp_body, resp_status, resp_headers = await handle_delete_script(param, r2_bucket=r2_bucket)
        resp_headers.update(rate_headers)
        return resp_body, resp_status, resp_headers

    if route == "/cron/jobs" and method == "GET":
        resp_body, resp_status, resp_headers = await handle_get_cron_jobs(r2_bucket=r2_bucket)
        resp_headers.update(rate_headers)
        return resp_body, resp_status, resp_headers

    if route == "/cron/jobs" and method == "PUT":
        resp_body, resp_status, resp_headers = await handle_put_cron_jobs(body, r2_bucket=r2_bucket)
        resp_headers.update(rate_headers)
        return resp_body, resp_status, resp_headers

    if route == "/cron/run" and method == "POST":
        resp_body, resp_status, resp_headers = await handle_cron_run(body, r2_bucket=r2_bucket)
        resp_headers.update(rate_headers)
        return resp_body, resp_status, resp_headers

    if route == "/feed/refresh" and method == "POST":
        resp_body, resp_status, resp_headers = await handle_feed_refresh(body, r2_bucket=r2_bucket)
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
            "version": "0.5.0",
            "features": {
                "modes": ["interpret", "compile", "auto"],
                "scripts": True,
                "cron": True,
                "alerts": True,
                "live_feed": True,
                "feed_sources": ["bybit", "binance"],
                "timeframes": ["1m", "5m", "15m", "30m", "1h", "4h", "1d"],
            },
        }
    )


# ---------------------------------------------------------------------------
# POST /run
# ---------------------------------------------------------------------------

_VALID_MODES = frozenset({"interpret", "compile", "auto"})


async def handle_run(
    body: str | None,
    r2_bucket: Any = None,
) -> tuple[dict[str, Any], int, dict[str, str]]:
    """Execute a Pine Script strategy over OHLCV data.

    Body fields:
      - ``script`` (str) — Pine source, OR
      - ``script_id`` (str) — deployed script id from R2 registry
      - ``ohlcv`` / ``data`` (list) — bars, or resolve from R2 via symbol+timeframe
      - ``mode`` — ``interpret`` | ``compile`` | ``auto`` (default ``interpret``)
      - ``max_bars`` — optional tail length when loading from R2
    """
    data, err = _parse_body(body)
    if err is not None:
        return err

    if data is None:
        return _json_response({"error": "Failed to parse request body"}, 400)

    script = data.get("script")
    script_id = data.get("script_id")
    deployed_id: str | None = None

    if (not script or not isinstance(script, str)) and script_id and isinstance(script_id, str):
        if r2_bucket is None:
            return _json_response({"error": "R2 bucket not configured — cannot load script_id"}, 501)
        try:
            from scripts_registry import get_script

            rec = await get_script(r2_bucket, script_id)
        except Exception as e:
            return _json_response({"error": f"Failed to load script: {e!s}"}, 502)
        if rec is None or not rec.get("script"):
            return _json_response({"error": f"Script not found: {script_id}"}, 404)
        script = rec["script"]
        deployed_id = script_id
        # Fill defaults from registry when client omitted them
        data.setdefault("symbol", rec.get("symbol", "BTCUSDT"))
        data.setdefault("timeframe", rec.get("timeframe", "1m"))
        data.setdefault("mode", rec.get("mode", "auto"))
        if "max_bars" not in data and rec.get("max_bars"):
            data["max_bars"] = rec["max_bars"]

    if not script or not isinstance(script, str):
        return _json_response(
            {"error": "Missing or invalid 'script' (or provide 'script_id')"},
            400,
        )

    if len(script) > _MAX_SCRIPT_LENGTH:
        return _json_response(
            {"error": f"Script exceeds {_MAX_SCRIPT_LENGTH} character limit"},
            413,
        )

    mode_raw = data.get("mode", "interpret")
    mode = str(mode_raw or "interpret").strip().lower()
    if mode not in _VALID_MODES:
        return _json_response(
            {"error": f"Invalid mode {mode_raw!r}; use interpret|compile|auto"},
            400,
        )

    ohlcv = _normalize_ohlcv(data)
    symbol: str = data.get("symbol", "BTCUSDT")
    timeframe: str = data.get("timeframe", "1d")
    if not isinstance(symbol, str):
        symbol = "BTCUSDT"
    if not isinstance(timeframe, str):
        timeframe = "1d"

    # Auto-fetch from R2 when symbol/timeframe explicit, or when script_id was used
    has_explicit_symbol = "symbol" in data and isinstance(data["symbol"], str)
    has_explicit_timeframe = "timeframe" in data and isinstance(data["timeframe"], str)
    should_fetch_r2 = ohlcv is None and (
        has_explicit_symbol or has_explicit_timeframe or deployed_id is not None
    )

    if should_fetch_r2:
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
                "Preload via POST /ingest (e.g. timeframe=1m), or provide 'ohlcv'/'data' inline.",
            }
        )

    max_bars = data.get("max_bars")
    if max_bars is not None:
        try:
            mb = int(max_bars)
            if mb > 0 and len(ohlcv) > mb:
                ohlcv = ohlcv[-mb:]
        except (TypeError, ValueError):
            return _json_response({"error": "'max_bars' must be an integer"}, 400)

    # Validate OHLCV
    validation_err = _validate_ohlcv(ohlcv)
    if validation_err:
        return _json_response({"error": validation_err}, 400)

    # Optional Pine input.* overrides (force interpret under mode=auto)
    inputs_raw = data.get("inputs")
    inputs: dict[str, Any] | None = None
    if isinstance(inputs_raw, dict) and inputs_raw:
        inputs = inputs_raw

    # Execute with timeout via Runtime
    runtime = Runtime(symbol=symbol)
    result = runtime.run(
        script,
        ohlcv,
        timeout_seconds=30.0,
        mode=mode,
        inputs=inputs,
    )

    if "error" in result:
        status = 504 if "timed out" in result.get("error", "").lower() else 500
        err_body: dict[str, Any] = {"error": result["error"]}
        if result.get("error_kind"):
            err_body["error_kind"] = result["error_kind"]
        if result.get("error_type"):
            err_body["error_type"] = result["error_type"]
        if result.get("error_bar") is not None:
            err_body["error_bar"] = result["error_bar"]
        return _json_response(err_body, status)

    if result.get("timed_out"):
        return _json_response(
            {"error": "Script execution timed out", "error_kind": "runtime"},
            504,
        )

    resp: dict[str, Any] = {
        "events": result.get("events", []),
        "plots": result.get("plots", []),
        "bars": result.get("count", len(ohlcv)),
        "script_id": result.get("script_id", ""),
        "run_id": result.get("run_id", ""),
        "mode": result.get("mode") or mode,
        "symbol": symbol,
        "timeframe": timeframe,
    }
    if deployed_id:
        resp["deployed_script_id"] = deployed_id
    if result.get("auto_backend"):
        resp["auto_backend"] = result["auto_backend"]
    if result.get("compile_fallback_reason"):
        resp["compile_fallback_reason"] = result["compile_fallback_reason"]
    if result.get("object_mode") is not None:
        resp["object_mode"] = result["object_mode"]
    if result.get("series") is not None:
        resp["series"] = result["series"]
    if result.get("drawings") is not None:
        resp["drawings"] = result["drawings"]
    if result.get("alerts") is not None:
        resp["alerts"] = result["alerts"]
    if result.get("alert_conditions") is not None:
        resp["alert_conditions"] = result["alert_conditions"]
    if result.get("meta") is not None:
        resp["meta"] = result["meta"]
    if result.get("compile_cached") is not None:
        resp["compile_cached"] = result["compile_cached"]
    return _json_response(resp)


# ---------------------------------------------------------------------------
# Scripts registry
# ---------------------------------------------------------------------------


async def handle_list_scripts(
    r2_bucket: Any = None,
) -> tuple[dict[str, Any], int, dict[str, str]]:
    if r2_bucket is None:
        return _json_response({"error": "R2 bucket not configured"}, 501)
    try:
        from scripts_registry import list_scripts

        items = await list_scripts(r2_bucket)
    except Exception as e:
        return _json_response({"error": f"Failed to list scripts: {e!s}"}, 500)
    return _json_response({"scripts": items, "count": len(items)})


async def handle_put_script(
    body: str | None,
    r2_bucket: Any = None,
) -> tuple[dict[str, Any], int, dict[str, str]]:
    if r2_bucket is None:
        return _json_response({"error": "R2 bucket not configured"}, 501)
    data, err = _parse_body(body)
    if err is not None:
        return err
    if data is None:
        return _json_response({"error": "Failed to parse request body"}, 400)
    try:
        from scripts_registry import put_script

        stored = await put_script(r2_bucket, data)
    except ValueError as e:
        return _json_response({"error": str(e)}, 400)
    except Exception as e:
        return _json_response({"error": f"Failed to store script: {e!s}"}, 500)
    return _json_response({"script": stored, "status": "ok"})


async def handle_get_script(
    script_id: str,
    r2_bucket: Any = None,
) -> tuple[dict[str, Any], int, dict[str, str]]:
    if r2_bucket is None:
        return _json_response({"error": "R2 bucket not configured"}, 501)
    try:
        from scripts_registry import get_script

        rec = await get_script(r2_bucket, script_id)
    except Exception as e:
        return _json_response({"error": f"Failed to load script: {e!s}"}, 500)
    if rec is None or rec.get("deleted"):
        return _json_response({"error": f"Script not found: {script_id}"}, 404)
    return _json_response({"script": rec})


async def handle_delete_script(
    script_id: str,
    r2_bucket: Any = None,
) -> tuple[dict[str, Any], int, dict[str, str]]:
    if r2_bucket is None:
        return _json_response({"error": "R2 bucket not configured"}, 501)
    try:
        from scripts_registry import delete_script

        ok = await delete_script(r2_bucket, script_id)
    except Exception as e:
        return _json_response({"error": f"Failed to delete script: {e!s}"}, 500)
    if not ok:
        return _json_response({"error": f"Script not found: {script_id}"}, 404)
    return _json_response({"deleted": script_id, "status": "ok"})


# ---------------------------------------------------------------------------
# Cron / schedules
# ---------------------------------------------------------------------------


async def handle_get_cron_jobs(
    r2_bucket: Any = None,
) -> tuple[dict[str, Any], int, dict[str, str]]:
    if r2_bucket is None:
        return _json_response({"error": "R2 bucket not configured"}, 501)
    try:
        from scripts_registry import load_cron_jobs

        jobs = await load_cron_jobs(r2_bucket)
    except Exception as e:
        return _json_response({"error": f"Failed to load cron jobs: {e!s}"}, 500)
    return _json_response({"jobs": jobs, "count": len(jobs)})


async def handle_put_cron_jobs(
    body: str | None,
    r2_bucket: Any = None,
) -> tuple[dict[str, Any], int, dict[str, str]]:
    if r2_bucket is None:
        return _json_response({"error": "R2 bucket not configured"}, 501)
    data, err = _parse_body(body)
    if err is not None:
        return err
    if data is None:
        return _json_response({"error": "Failed to parse request body"}, 400)
    jobs = data.get("jobs")
    if not isinstance(jobs, list):
        return _json_response({"error": "Body must include 'jobs' array"}, 400)
    try:
        from scripts_registry import put_cron_jobs

        stored = await put_cron_jobs(r2_bucket, jobs)
    except Exception as e:
        return _json_response({"error": f"Failed to store cron jobs: {e!s}"}, 500)
    return _json_response({"jobs": stored, "count": len(stored), "status": "ok"})


async def handle_cron_run(
    body: str | None,
    r2_bucket: Any = None,
) -> tuple[dict[str, Any], int, dict[str, str]]:
    """Manually trigger the bar-close scheduler (same as Cron Trigger)."""
    force = False
    refresh_market = True
    if body and body.strip():
        data, err = _parse_body(body)
        if err is not None:
            return err
        if data is not None:
            force = bool(data.get("force", False))
            if "refresh_market" in data:
                refresh_market = bool(data.get("refresh_market"))
    try:
        from scheduler import run_scheduled_jobs

        summary = await run_scheduled_jobs(
            r2_bucket,
            force=force,
            refresh_market=refresh_market,
        )
    except Exception as e:
        return _json_response({"error": f"Cron run failed: {e!s}"}, 500)
    if summary.get("error") and not summary.get("results"):
        return _json_response(summary, 501)
    return _json_response(summary)


async def handle_feed_refresh(
    body: str | None,
    r2_bucket: Any = None,
) -> tuple[dict[str, Any], int, dict[str, str]]:
    """Pull latest klines from Bybit (etc.) into R2.

    Body (all optional)::

        {
          "symbol": "BTCUSDT",      // or omit to refresh all cron job pairs
          "timeframe": "1m",
          "limit": 200
        }
    """
    if r2_bucket is None:
        return _json_response({"error": "R2 bucket not configured"}, 501)

    symbol: str | None = None
    timeframe: str = "1m"
    limit = 200
    if body and body.strip():
        data, err = _parse_body(body)
        if err is not None:
            return err
        if data is not None:
            if data.get("symbol"):
                symbol = str(data["symbol"]).upper()
            if data.get("timeframe"):
                timeframe = str(data["timeframe"])
            if data.get("limit") is not None:
                try:
                    limit = int(data["limit"])
                except (TypeError, ValueError):
                    return _json_response({"error": "'limit' must be an integer"}, 400)

    try:
        from market_feed import refresh_pair_to_r2
        from market_feed import refresh_pairs_for_jobs
        from scripts_registry import load_cron_jobs

        if symbol:
            info = await refresh_pair_to_r2(
                r2_bucket, symbol, timeframe, limit=limit, closed_only=True
            )
            return _json_response({"status": "ok", "feed": [info]})

        jobs = await load_cron_jobs(r2_bucket)
        if not jobs:
            # No deployed jobs — still refresh a sensible default
            info = await refresh_pair_to_r2(
                r2_bucket, "BTCUSDT", "1m", limit=limit, closed_only=True
            )
            return _json_response({"status": "ok", "feed": [info], "note": "no cron jobs; refreshed BTCUSDT/1m"})

        feed = await refresh_pairs_for_jobs(r2_bucket, jobs, limit=limit)
        return _json_response({"status": "ok", "feed": feed})
    except Exception as e:
        return _json_response({"error": f"Feed refresh failed: {e!s}"}, 502)


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
