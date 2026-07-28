# Copyright (c) 2026 HOOX · PYNE · jango-blockchained
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Cron bar-close scheduler for deployed scripts.

On each scheduled wake (default every 1 minute):

1. Load enabled cron jobs from R2
2. **Refresh market data** (Bybit → R2) for each job's symbol/timeframe
3. For each job, load script + OHLCV from R2
4. If the latest **closed** bar time advanced since last run → execute
5. Persist last bar time and collect events for trade-forwarding
"""

from __future__ import annotations

from typing import Any
from typing import Awaitable
from typing import Callable

from data_provider import fetch_ohlcv_from_r2
from market_feed import refresh_pairs_for_jobs
from pynescript_backend import Runtime
from scripts_registry import get_cron_state
from scripts_registry import get_script
from scripts_registry import load_cron_jobs
from scripts_registry import put_cron_state

HttpGetJson = Callable[[str], Awaitable[Any]]


def _job_key(job: dict[str, Any]) -> str:
    sid = str(job.get("script_id") or "unknown")
    sym = str(job.get("symbol") or "BTCUSDT").upper()
    tf = str(job.get("timeframe") or "1m")
    return f"{sid}:{sym}:{tf}"


async def run_scheduled_jobs(
    r2_bucket: Any,
    *,
    force: bool = False,
    refresh_market: bool = True,
    feed_limit: int = 200,
    http_get_json: HttpGetJson | None = None,
) -> dict[str, Any]:
    """Execute due cron jobs (bar-close semantics).

    Args:
        r2_bucket: R2 binding with scripts + OHLCV.
        force: If True, run even when bar time has not advanced (testing).
        refresh_market: If True (default), pull latest klines into R2 first.
        feed_limit: How many recent candles to pull per pair.
        http_get_json: Optional injectable HTTP client (tests).

    Returns:
        Summary dict with per-job results, feed status, and aggregate events.
    """
    if r2_bucket is None:
        return {"error": "R2 bucket not configured", "jobs": [], "events": [], "feed": []}

    jobs = await load_cron_jobs(r2_bucket)

    # --- Fresh data first (otherwise cron is useless) --------------------
    feed_results: list[dict[str, Any]] = []
    if refresh_market and jobs:
        feed_kwargs: dict[str, Any] = {"limit": feed_limit}
        if http_get_json is not None:
            feed_kwargs["http_get_json"] = http_get_json
        try:
            feed_results = await refresh_pairs_for_jobs(r2_bucket, jobs, **feed_kwargs)
        except Exception as e:
            feed_results = [{"status": "error", "error": str(e)}]

    results: list[dict[str, Any]] = []
    all_events: list[dict[str, Any]] = []

    for job in jobs:
        if not job.get("enabled", True):
            results.append(
                {
                    "script_id": job.get("script_id"),
                    "status": "skipped",
                    "reason": "disabled",
                }
            )
            continue

        script_id = str(job.get("script_id"))
        symbol = str(job.get("symbol") or "BTCUSDT").upper()
        timeframe = str(job.get("timeframe") or "1m")
        mode = str(job.get("mode") or "auto").strip().lower()
        max_bars = int(job.get("max_bars") or 5000)
        jkey = _job_key(job)

        rec = await get_script(r2_bucket, script_id)
        if rec is None or not rec.get("script"):
            results.append(
                {
                    "script_id": script_id,
                    "status": "error",
                    "reason": "script not found",
                }
            )
            continue

        # Job fields override stored defaults when present
        script = str(rec["script"])
        mode = str(job.get("mode") or rec.get("mode") or "auto").strip().lower()
        symbol = str(job.get("symbol") or rec.get("symbol") or symbol).upper()
        timeframe = str(job.get("timeframe") or rec.get("timeframe") or timeframe)
        max_bars = int(job.get("max_bars") or rec.get("max_bars") or max_bars)
        forward = bool(job.get("forward_events", rec.get("forward_events", True)))

        try:
            ohlcv = await fetch_ohlcv_from_r2(r2_bucket, symbol, timeframe)
        except Exception as e:
            results.append(
                {
                    "script_id": script_id,
                    "status": "error",
                    "reason": f"r2 read failed: {e!s}",
                }
            )
            continue

        if not ohlcv:
            results.append(
                {
                    "script_id": script_id,
                    "status": "skipped",
                    "reason": f"no data for {symbol}/{timeframe}",
                }
            )
            continue

        if len(ohlcv) > max_bars:
            ohlcv = ohlcv[-max_bars:]

        last_bar = ohlcv[-1]
        last_bar_time = int(last_bar.get("time") or 0)

        state = await get_cron_state(r2_bucket, jkey)
        prev_time = int(state.get("last_bar_time") or 0)

        if not force and last_bar_time > 0 and last_bar_time <= prev_time:
            results.append(
                {
                    "script_id": script_id,
                    "status": "skipped",
                    "reason": "no new bar",
                    "last_bar_time": last_bar_time,
                }
            )
            continue

        runtime = Runtime(symbol=symbol)
        result = runtime.run(script, ohlcv, timeout_seconds=25.0, mode=mode)

        if "error" in result:
            results.append(
                {
                    "script_id": script_id,
                    "status": "error",
                    "reason": result["error"],
                    "mode": result.get("mode") or mode,
                }
            )
            continue

        events = result.get("events") or []
        if isinstance(events, list) and events and forward:
            # Tag for trade-forwarder
            for ev in events:
                if isinstance(ev, dict):
                    ev.setdefault("symbol", symbol)
                    ev.setdefault("timeframe", timeframe)
                    ev.setdefault("deployed_script_id", script_id)
            all_events.extend(ev for ev in events if isinstance(ev, dict))

        await put_cron_state(
            r2_bucket,
            jkey,
            {
                "last_bar_time": last_bar_time,
                "last_run_at": last_bar_time,
                "bars": result.get("count", len(ohlcv)),
                "mode": result.get("mode") or mode,
                "auto_backend": result.get("auto_backend"),
            },
        )

        results.append(
            {
                "script_id": script_id,
                "status": "ok",
                "symbol": symbol,
                "timeframe": timeframe,
                "bars": result.get("count", len(ohlcv)),
                "last_bar_time": last_bar_time,
                "events": len(events) if isinstance(events, list) else 0,
                "mode": result.get("mode") or mode,
                "auto_backend": result.get("auto_backend"),
                "forward_events": forward,
            }
        )

    return {
        "jobs_run": len([r for r in results if r.get("status") == "ok"]),
        "jobs_skipped": len([r for r in results if r.get("status") == "skipped"]),
        "jobs_error": len([r for r in results if r.get("status") == "error"]),
        "results": results,
        "events": all_events,
        "feed": feed_results,
    }
