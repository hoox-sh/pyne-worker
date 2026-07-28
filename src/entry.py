# Copyright (c) 2026 HOOX · PYNE · jango-blockchained
# SPDX-License-Identifier: AGPL-3.0-or-later

"""pyne-worker — Python Cloudflare Worker for Pine Script evaluation.

Runs TradingView Pine Script via the pynescript evaluator / compile path and
emits structured trade events.

Production features:
- API key authentication (``X-API-Key`` header / ``API_KEY`` secret)
- In-memory rate limiting (sliding window)
- Payload size & input validation
- Structured per-request logging
- Dependency health checks (R2, trade-worker)
- Execution timeout (30 s)
- R2 data ingestion (``POST /ingest``)
- Deployed script registry (``POST /scripts``)
- Cron bar-close scheduler (``scheduled`` + ``POST /cron/run``)
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
import time
from urllib.parse import urlparse

from workers import Response
from workers import WorkerEntrypoint

from handler import handle_request
from middleware import LogHelper
from trade_forwarder import forward_events


class Default(WorkerEntrypoint):
    """Cloudflare Worker entry point.

    Expected environment bindings / secrets:

    - ``TRADE_SERVICE`` — Service binding to trade-worker.
    - ``OHLCV_DATA`` — R2 bucket for OHLCV bar data.
    - ``API_KEY`` (secret) — Expected API key for auth.
      When unset, auth is disabled (dev mode).
    """

    TRADE_SERVICE: object  # service binding, set by the runtime
    OHLCV_DATA: object  # R2 bucket binding, set by the runtime
    API_KEY: str | None = None  # secret, set by the runtime

    async def fetch(self, request):
        start = time.time()
        request_id = LogHelper.make_request_id()
        path = urlparse(request.url).path or "/"
        method = request.method

        # Read optional auth header
        api_key = request.headers.get("X-API-Key", None)

        # Read body for POST
        body = None
        if method == "POST":
            raw = await request.text()
            # Only parse if non-empty
            if raw.strip():
                body = raw

        payload, status, extra_headers = await handle_request(
            method,
            path,
            body=body,
            r2_bucket=getattr(self.env, "OHLCV_DATA", None),
            api_key=api_key,
            expected_api_key=getattr(self.env, "API_KEY", None),
            request_id=request_id,
        )

        # -- Forward strategy events to trade-worker ----------------------
        events = payload.get("events", [])
        # Allow client to opt out of forwarding with "forward_events": false
        _forward_flag: bool = True
        if body:
            try:
                _parsed = json.loads(body)
                if isinstance(_parsed, dict):
                    _forward_flag = _parsed.get("forward_events", True)
            except json.JSONDecodeError:
                pass
        if events and hasattr(self.env, "TRADE_SERVICE") and _forward_flag:
            symbol = payload.get("symbol", "BTCUSDT")
            try:
                fwd = await forward_events(
                    events,
                    self.env.TRADE_SERVICE,
                    symbol=symbol,
                )
                if fwd.get("failed", 0) > 0:
                    payload["forward_errors"] = fwd.get("errors", [])
                    payload["forwarded"] = fwd.get("forwarded", 0)
                    payload["forward_failed"] = fwd.get("failed", 0)
            except Exception as e:
                payload["forward_error"] = str(e)

        # -- Enhanced health dependency checks (async) --------------------
        if path == "/health" and method == "GET":
            try:
                from handler import _check_deps_async

                deps = await _check_deps_async(
                    getattr(self.env, "OHLCV_DATA", None),
                )
                payload["dependencies"] = deps
            except Exception as e:
                payload["dependencies"] = {"error": str(e)}

        # -- Structured log entry -----------------------------------------
        duration_ms = (time.time() - start) * 1000
        log = LogHelper.as_dict(
            request_id=request_id,
            method=method,
            path=path,
            status=status,
            duration_ms=duration_ms,
            bars=payload.get("bars", payload.get("ingested")),
            events=len(payload.get("events", [])),
        )
        # In production this would go to a structured logging sink;
        # for now we emit via the Workers console.
        print(json.dumps(log))

        # -- Build response -----------------------------------------------
        headers = {"Content-Type": "application/json"}
        headers.update(extra_headers)
        if request_id:
            headers["X-Request-ID"] = request_id

        return Response(
            json.dumps(payload),
            status=status,
            headers=headers,
        )

    async def scheduled(self, controller, env, ctx):
        """Cron Trigger entry — bar-close run for deployed scripts.

        Configure in wrangler.jsonc::

            "triggers": { "crons": ["* * * * *"] }

        Each minute:
          1. Pull latest closed klines (Bybit primary → R2)
          2. Re-run each enabled deployed script only if bar time advanced
        """
        start = time.time()
        request_id = LogHelper.make_request_id()
        cron_expr = getattr(controller, "cron", None) or "* * * * *"

        try:
            from scheduler import run_scheduled_jobs

            summary = await run_scheduled_jobs(
                getattr(self.env, "OHLCV_DATA", None),
                force=False,
                refresh_market=True,
            )
        except Exception as e:
            summary = {"error": str(e), "jobs": [], "events": [], "feed": []}

        # Forward strategy events to trade-worker
        events = summary.get("events") or []
        if events and hasattr(self.env, "TRADE_SERVICE"):
            # Group by symbol when present
            by_symbol: dict[str, list] = {}
            for ev in events:
                if not isinstance(ev, dict):
                    continue
                sym = str(ev.get("symbol") or "BTCUSDT")
                by_symbol.setdefault(sym, []).append(ev)
            forward_meta: dict[str, object] = {}
            for sym, evs in by_symbol.items():
                try:
                    fwd = await forward_events(evs, self.env.TRADE_SERVICE, symbol=sym)
                    forward_meta[sym] = fwd
                except Exception as e:
                    forward_meta[sym] = {"error": str(e)}
            summary["forward"] = forward_meta

        duration_ms = (time.time() - start) * 1000
        log = LogHelper.as_dict(
            request_id=request_id,
            method="SCHEDULED",
            path=f"cron:{cron_expr}",
            status=200 if "error" not in summary else 500,
            duration_ms=duration_ms,
            bars=summary.get("jobs_run"),
            events=len(events) if isinstance(events, list) else 0,
        )
        print(json.dumps(log))
        print(json.dumps({"cron_summary": summary, "request_id": request_id}))
