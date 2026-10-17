#!/usr/bin/env python3
# Copyright (c) 2026 HOOX · PYNE · jango-blockchained
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Live end-to-end suite + latency probe for a running pyne-worker.

Unlike ``pytest tests/`` (in-process handler), every case here is a real HTTP
request against a deployed Worker or ``wrangler dev``. Each request records
wall-clock latency; ``/run`` cases also record the engine-reported
``profile.total_ms`` so edge overhead is visible next to engine time.

Coverage: health/feature flags, auth, interpret / strategy events / alerts /
drawings / inputs / libraries, response windowing (+ validation), auto &
compile modes, parse + timeout error contracts, payload cap, R2 ingest →
read-back, Bybit feed refresh, scripts registry lifecycle, cron jobs listing,
and an opt-in cron run.

Usage::

    # Production (reads API_KEY from env or .dev.vars):
    PYNE_API_KEY=… .venv/bin/python scripts/live_test.py

    # Local wrangler dev:
    .venv/bin/python scripts/live_test.py --url http://127.0.0.1:8787

    # More latency samples + machine-readable report:
    .venv/bin/python scripts/live_test.py --repeat 5 --json live_report.json

Exit codes: ``0`` all passed · ``1`` any failure · ``2`` config error.

Side effects (all opt-out): the suite writes one clearly-named R2 test symbol
(``LIVETEST``), refreshes a few BTCUSDT 1m bars, and creates + deletes a
temporary script record (``enabled: false``). Use ``--no-r2``, ``--no-feed``,
``--no-scripts`` to keep the target untouched. ``--with-cron`` additionally
runs the scheduler (can fire alert webhooks — off by default).
"""

from __future__ import annotations

import argparse
import json
import math
import os
import statistics
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from typing import Callable

DEFAULT_URL = "https://pyne-worker.cryptolinx.workers.dev"
REPO_ROOT = Path(__file__).resolve().parent.parent
USER_AGENT = "pyne-worker-live-test/0.7.0"

# Fixed fixture clock — 2026-01-01T00:00:00Z. Deterministic (identical bars on
# every run → R2 ingest dedupes by time) and inside the reader's ~10-year scan.
BARS_BASE_MS = 1_767_225_600_000
R2TEST_SYMBOL = "LIVETEST"
R2TEST_TIMEFRAME = "1m"
R2TEST_BARS = 60

# ---------------------------------------------------------------------------
# Fixtures (validated against the real handler in tests/probe)
# ---------------------------------------------------------------------------


def make_bars(n: int = 200, base_ms: int = BARS_BASE_MS, step_ms: int = 60_000) -> list[dict[str, Any]]:
    """Deterministic OHLCV: sine wave (~8 strategy crossovers over 200 bars)."""
    bars: list[dict[str, Any]] = []
    prev = 100.0
    for i in range(n):
        close = 100.0 + 10.0 * math.sin(i / 7.0)
        bars.append(
            {
                "open": prev,
                "high": max(prev, close) + 0.5,
                "low": min(prev, close) - 0.5,
                "close": close,
                "volume": 1000 + i,
                "time": base_ms + i * step_ms,
            }
        )
        prev = close
    return bars


SIMPLE = "//@version=6\nindicator('live-simple')\nplot(close)\nplot(ta.sma(close, 5))"
STRATEGY = (
    "//@version=6\nstrategy('live-strategy', overlay=true)\n"
    "fast = ta.sma(close, 5)\nslow = ta.sma(close, 20)\n"
    "if ta.crossover(fast, slow)\n    strategy.entry('long', strategy.long)\n"
    "if ta.crossunder(fast, slow)\n    strategy.close('long')"
)
ALERTS = (
    "//@version=6\nindicator('live-alerts')\n"
    "alertcondition(ta.crossover(close, ta.sma(close, 10)), 'X', 'cross')\n"
    "alert('manual ping', alert.freq_once_per_bar_close)\nplot(close)"
)
DRAWINGS = (
    "//@version=6\nindicator('live-drawings', overlay=true)\n"
    "if bar_index % 25 == 0\n"
    "    label.new(bar_index, high, 'L' + str.tostring(bar_index))\n"
    "    line.new(bar_index - 5, low, bar_index, high)\nplot(close)"
)
LIBRARY_SRC = "//@version=6\nlibrary('LiveLib')\nexport const float FOO = 1.5"
USE_LIBRARY = "//@version=6\nindicator('live-lib')\nimport ns/LiveLib/1 as x\nplot(x.FOO)"
INPUTS_SCRIPT = (
    "//@version=6\nindicator('live-inputs')\n"
    "len = input.int(20, 'Length')\nplot(ta.sma(close, len))"
)

# ---------------------------------------------------------------------------
# HTTP layer
# ---------------------------------------------------------------------------


@dataclass
class HttpResult:
    status: int | None
    body: dict[str, Any] | None
    ms: float
    error: str | None = None
    attempts: int = 1


def _retry_after_seconds(headers: Any) -> float:
    try:
        raw = headers.get("Retry-After") if headers else None
        if raw:
            return min(max(float(raw), 1.0), 30.0)
    except (TypeError, ValueError):
        pass
    return 2.0


class Client:
    """Minimal JSON client with per-request latency and 429 back-off."""

    def __init__(self, url: str, api_key: str | None, timeout: float) -> None:
        self.base = url.rstrip("/")
        self.api_key = api_key
        self.timeout = timeout
        self.retries = 0

    def request(
        self,
        method: str,
        path: str,
        body: dict[str, Any] | None = None,
        *,
        auth: bool = True,
        key: str | None = None,
    ) -> HttpResult:
        data = json.dumps(body, separators=(",", ":")).encode() if body is not None else None
        headers = {"Accept": "application/json", "User-Agent": USER_AGENT}
        if data is not None:
            headers["Content-Type"] = "application/json"
        send_key = key if key is not None else (self.api_key if auth else None)
        if send_key:
            headers["X-API-Key"] = send_key

        attempts = 0
        while True:
            attempts += 1
            started = time.perf_counter()
            try:
                req = urllib.request.Request(self.base + path, data=data, headers=headers, method=method)
                with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                    raw = resp.read()
                    status: int | None = resp.status
                    hdrs = resp.headers
            except urllib.error.HTTPError as exc:
                raw = exc.read()
                status = exc.code
                hdrs = exc.headers
            except Exception as exc:  # noqa: BLE001 — URLError/timeout/etc.
                ms = (time.perf_counter() - started) * 1000
                return HttpResult(None, None, ms, f"{type(exc).__name__}: {exc}", attempts)

            ms = (time.perf_counter() - started) * 1000
            parsed: dict[str, Any] | None = None
            if raw:
                try:
                    loaded = json.loads(raw)
                    parsed = loaded if isinstance(loaded, dict) else {"_list": loaded}
                except json.JSONDecodeError:
                    parsed = None

            if status == 429 and attempts < 3:
                self.retries += 1
                time.sleep(_retry_after_seconds(hdrs))
                continue
            return HttpResult(status, parsed, ms, None, attempts)


# ---------------------------------------------------------------------------
# Case runner
# ---------------------------------------------------------------------------


@dataclass
class Outcome:
    ok: bool
    detail: str = ""
    extra: dict[str, Any] = field(default_factory=dict)

    @staticmethod
    def passed(detail: str = "", **extra: Any) -> Outcome:
        return Outcome(True, detail, extra)

    @staticmethod
    def failed(detail: str = "", **extra: Any) -> Outcome:
        return Outcome(False, detail, extra)


@dataclass
class Case:
    name: str
    fn: Callable[[Suite], Outcome]
    repeat: int = 1
    skip_reason: str | None = None


@dataclass
class CaseReport:
    name: str
    status: str  # PASS | FAIL | SKIP
    http: int | None = None
    detail: str = ""
    latencies: list[float] = field(default_factory=list)
    engine_ms: list[float] = field(default_factory=list)
    extra: dict[str, Any] = field(default_factory=dict)


def _engine_ms(r: HttpResult) -> float | None:
    body = r.body or {}
    profile = body.get("profile")
    if isinstance(profile, dict) and isinstance(profile.get("total_ms"), (int, float)):
        return float(profile["total_ms"])
    return None


def _error_of(r: HttpResult) -> str:
    if r.error:
        return r.error
    body = r.body or {}
    return str(body.get("error") or f"HTTP {r.status}")


class Suite:
    def __init__(self, client: Client, default_repeat: int, quiet: bool = False) -> None:
        self.client = client
        self.default_repeat = default_repeat
        self.quiet = quiet
        self.cases: list[Case] = []
        self.all_latencies: list[float] = []
        self.requests = 0
        self._cur: CaseReport | None = None
        self.health: dict[str, Any] = {}

    # -- registration -----------------------------------------------------
    def add(
        self,
        name: str,
        fn: Callable[[Suite], Outcome],
        *,
        repeat: int | None = None,
        skip_reason: str | None = None,
    ) -> None:
        self.cases.append(
            Case(name=name, fn=fn, repeat=self.default_repeat if repeat is None else repeat, skip_reason=skip_reason)
        )

    # -- used by case functions -------------------------------------------
    def request(
        self,
        method: str,
        path: str,
        body: dict[str, Any] | None = None,
        *,
        auth: bool = True,
        key: str | None = None,
    ) -> HttpResult:
        r = self.client.request(method, path, body, auth=auth, key=key)
        assert self._cur is not None
        self._cur.latencies.append(r.ms)
        self._cur.http = r.status
        self.all_latencies.append(r.ms)
        self.requests += 1
        return r

    def engine(self, ms: float | None) -> None:
        if ms is not None and self._cur is not None:
            self._cur.engine_ms.append(ms)

    # -- execution --------------------------------------------------------
    def run(self) -> list[CaseReport]:
        reports: list[CaseReport] = []
        for case in self.cases:
            if case.skip_reason:
                rep = CaseReport(name=case.name, status="SKIP", detail=case.skip_reason)
                reports.append(rep)
                if not self.quiet:
                    _emit(rep)
                continue
            rep = CaseReport(name=case.name, status="FAIL", http=None)
            self._cur = rep
            outcome = Outcome.failed("case produced no result")
            for _ in range(max(1, case.repeat)):
                try:
                    outcome = case.fn(self)
                except Exception as exc:  # noqa: BLE001 — a broken case must not kill the suite
                    outcome = Outcome.failed(f"unexpected {type(exc).__name__}: {exc}")
                if not outcome.ok:
                    break
            rep.detail = outcome.detail
            rep.extra = outcome.extra
            rep.status = "PASS" if outcome.ok else "FAIL"
            reports.append(rep)
            if not self.quiet or rep.status == "FAIL":
                _emit(rep)
        self._cur = None
        return reports


# ---------------------------------------------------------------------------
# Cases (order matters: health first so the report header has version data)
# ---------------------------------------------------------------------------


def case_health(s: Suite) -> Outcome:
    r = s.request("GET", "/health", auth=False)
    if r.status != 200:
        return Outcome.failed(_error_of(r))
    body = r.body or {}
    if body.get("status") != "ok":
        return Outcome.failed(f"status={body.get('status')!r}")
    if body.get("worker") != "pyne-worker":
        return Outcome.failed(f"worker={body.get('worker')!r}")
    features = body.get("features") or {}
    missing = [k for k in ("scripts", "cron", "alerts", "libraries", "windowing") if k not in features]
    if missing:
        return Outcome.failed("feature flags missing: " + ", ".join(missing))
    s.health = body
    r2 = (body.get("dependencies") or {}).get("r2", "?")
    return Outcome.passed(
        f"v{body.get('version')} · engine {body.get('engine')} · r2={r2}",
        version=body.get("version"),
        engine=body.get("engine"),
    )


def case_404(s: Suite) -> Outcome:
    r = s.request("GET", "/live-test-no-such-route", auth=False)
    if r.status != 404:
        return Outcome.failed(f"expected 404, got {r.status}")
    return Outcome.passed("unknown route → 404")


def case_auth_missing(s: Suite) -> Outcome:
    r = s.request("POST", "/run", {"script": SIMPLE, "ohlcv": make_bars(5)}, auth=False)
    if r.status == 401:
        return Outcome.passed("no key → 401")
    if r.status == 200:
        return Outcome.failed("200 without a key — API_KEY looks unset on the worker (dev mode)")
    return Outcome.failed(f"expected 401, got {r.status}: {_error_of(r)}")


def case_auth_wrong(s: Suite) -> Outcome:
    r = s.request("POST", "/run", {"script": SIMPLE, "ohlcv": make_bars(5)}, key="live-test-wrong-key")
    if r.status != 401:
        return Outcome.failed(f"expected 401, got {r.status}: {_error_of(r)}")
    return Outcome.passed("bad key → 401")


def case_run_interpret(s: Suite) -> Outcome:
    r = s.request(
        "POST",
        "/run",
        {
            "script": SIMPLE,
            "ohlcv": make_bars(200),
            "mode": "interpret",
            "symbol": R2TEST_SYMBOL,
            "timeframe": R2TEST_TIMEFRAME,
        },
    )
    s.engine(_engine_ms(r))
    if r.status != 200:
        return Outcome.failed(f"{r.status}: {_error_of(r)}")
    body = r.body or {}
    if body.get("status") != "success":
        return Outcome.failed(f"status={body.get('status')!r} error={body.get('error')!r}")
    if body.get("bars") != 200:
        return Outcome.failed(f"bars={body.get('bars')!r} (expected 200)")
    series = body.get("series") or {}
    plot0 = series.get("plot_0")
    if not isinstance(plot0, list) or len(plot0) != 200:
        return Outcome.failed(f"series.plot_0 length={len(plot0) if isinstance(plot0, list) else plot0!r}")
    if not body.get("plots"):
        return Outcome.failed("empty plots vector")
    profile = body.get("profile") or {}
    return Outcome.passed(
        f"bars=200 · plots={len(series)} series · mode={body.get('mode')} · engine {profile.get('total_ms')}ms "
        f"(parse {((profile.get('phases') or {}).get('parse_ms'))}ms)"
    )


def case_run_strategy(s: Suite) -> Outcome:
    r = s.request("POST", "/run", {"script": STRATEGY, "ohlcv": make_bars(200)})
    s.engine(_engine_ms(r))
    if r.status != 200:
        return Outcome.failed(f"{r.status}: {_error_of(r)}")
    events = (r.body or {}).get("events") or []
    if len(events) < 2:
        return Outcome.failed(f"expected strategy events, got {len(events)}")
    first = events[0] if isinstance(events[0], dict) else {}
    if "kind" not in first or not isinstance(first.get("bar_index"), int):
        return Outcome.failed(f"event shape unexpected: {json.dumps(first)[:120]}")
    kinds = sorted({e.get("kind") for e in events if isinstance(e, dict)})
    return Outcome.passed(f"events={len(events)} (kinds: {', '.join(str(k) for k in kinds)})")


def case_run_alerts(s: Suite) -> Outcome:
    r = s.request("POST", "/run", {"script": ALERTS, "ohlcv": make_bars(200)})
    s.engine(_engine_ms(r))
    if r.status != 200:
        return Outcome.failed(f"{r.status}: {_error_of(r)}")
    body = r.body or {}
    alerts = body.get("alerts") or []
    conditions = body.get("alert_conditions") or []
    if not alerts:
        return Outcome.failed("no alert firings exported")
    titled = [c for c in conditions if isinstance(c, dict) and c.get("title") == "X"]
    if not titled:
        return Outcome.failed("alertcondition 'X' not present in alert_conditions")
    return Outcome.passed(f"alerts={len(alerts)} · alert_conditions={len(conditions)} (title 'X' present)")


def case_run_drawings(s: Suite) -> Outcome:
    r = s.request("POST", "/run", {"script": DRAWINGS, "ohlcv": make_bars(200)})
    s.engine(_engine_ms(r))
    if r.status != 200:
        return Outcome.failed(f"{r.status}: {_error_of(r)}")
    drawings = (r.body or {}).get("drawings") or []
    if not drawings:
        return Outcome.failed("no drawings exported")
    types = sorted({d.get("type") for d in drawings if isinstance(d, dict)})
    return Outcome.passed(f"drawings={len(drawings)} (types: {', '.join(str(t) for t in types)})")


def case_run_window(s: Suite) -> Outcome:
    r = s.request("POST", "/run", {"script": SIMPLE, "ohlcv": make_bars(200), "from_bar": -10, "to_bar": -1})
    s.engine(_engine_ms(r))
    if r.status != 200:
        return Outcome.failed(f"{r.status}: {_error_of(r)}")
    body = r.body or {}
    window = body.get("window")
    expected = {"from_bar": 190, "to_bar": 199, "bars": 10, "total_bars": 200}
    if window != expected:
        return Outcome.failed(f"window={window!r} (expected {expected!r})")
    plot0 = (body.get("series") or {}).get("plot_0") or []
    if len(plot0) != 10:
        return Outcome.failed(f"series.plot_0 length={len(plot0)} (expected 10)")
    meta_profile = (body.get("meta") or {}).get("profile") or {}
    if not meta_profile.get("windowed") or meta_profile.get("bars") != 10:
        return Outcome.failed(f"meta.profile not marked windowed: {json.dumps(meta_profile)[:120]}")
    if (body.get("profile") or {}).get("bars") != 200:
        return Outcome.failed("top-level profile.bars must stay the full length (only meta.profile is windowed)")
    return Outcome.passed("200→10 bars · meta.profile.windowed · profile.bars stays 200")


def case_run_window_full(s: Suite) -> Outcome:
    r = s.request("POST", "/run", {"script": SIMPLE, "ohlcv": make_bars(50), "from_bar": 0, "to_bar": 49})
    s.engine(_engine_ms(r))
    if r.status != 200:
        return Outcome.failed(f"{r.status}: {_error_of(r)}")
    if "window" in (r.body or {}):
        return Outcome.failed("full-range window must be a no-op (no 'window' key)")
    return Outcome.passed("full-range request is a no-op")


def case_run_window_validation(s: Suite) -> Outcome:
    r = s.request("POST", "/run", {"script": SIMPLE, "ohlcv": make_bars(10), "from_bar": True})
    if r.status != 400:
        return Outcome.failed(f"expected 400 for boolean from_bar, got {r.status}")
    if "from_bar" not in str((r.body or {}).get("error") or ""):
        return Outcome.failed(f"unexpected error body: {_error_of(r)}")
    return Outcome.passed("boolean from_bar → 400")


def case_run_libraries(s: Suite) -> Outcome:
    r = s.request(
        "POST",
        "/run",
        {
            "script": USE_LIBRARY,
            "ohlcv": make_bars(20),
            "mode": "interpret",
            "libraries": [{"namespace": "ns", "name": "LiveLib", "version": 1, "source": LIBRARY_SRC}],
        },
    )
    s.engine(_engine_ms(r))
    if r.status != 200:
        return Outcome.failed(f"{r.status}: {_error_of(r)}")
    plot0 = ((r.body or {}).get("series") or {}).get("plot_0") or []
    if len(plot0) != 20 or plot0[0] != 1.5:
        return Outcome.failed(f"library export not plotted: first={plot0[:1]!r} len={len(plot0)}")
    return Outcome.passed("import ns/LiveLib/1 → FOO=1.5 plotted")


def case_run_inputs(s: Suite) -> Outcome:
    override = s.request(
        "POST",
        "/run",
        {"script": INPUTS_SCRIPT, "ohlcv": make_bars(30), "mode": "interpret", "inputs": {"Length": 5}},
    )
    s.engine(_engine_ms(override))
    if override.status != 200:
        return Outcome.failed(f"override run: {override.status}: {_error_of(override)}")
    default = s.request("POST", "/run", {"script": INPUTS_SCRIPT, "ohlcv": make_bars(30), "mode": "interpret"})
    s.engine(_engine_ms(default))
    if default.status != 200:
        return Outcome.failed(f"default run: {default.status}: {_error_of(default)}")

    def first_value(resp: HttpResult) -> Any:
        items = (resp.body or {}).get("inputs") or []
        return items[0].get("value") if items and isinstance(items[0], dict) else None

    if first_value(override) != 5 or first_value(default) != 20:
        return Outcome.failed(
            f"input values override={first_value(override)!r} default={first_value(default)!r} (expected 5 / 20)"
        )
    return Outcome.passed("input.int override honored (5) + default reported (20)")


def case_run_mode_auto(s: Suite) -> Outcome:
    r = s.request("POST", "/run", {"script": SIMPLE, "ohlcv": make_bars(100), "mode": "auto"})
    s.engine(_engine_ms(r))
    if r.status != 200:
        return Outcome.failed(f"{r.status}: {_error_of(r)}")
    body = r.body or {}
    backend = body.get("auto_backend")
    if backend not in ("interpret", "compile"):
        return Outcome.failed(f"auto_backend={backend!r}")
    fallback = body.get("compile_fallback_reason")
    note = f"backend={backend}" + (f" · fallback: {fallback}" if fallback else "")
    return Outcome.passed(note, auto_backend=backend, compile_fallback_reason=fallback)


def case_run_mode_compile(s: Suite) -> Outcome:
    r = s.request("POST", "/run", {"script": SIMPLE, "ohlcv": make_bars(100), "mode": "compile"})
    s.engine(_engine_ms(r))
    if r.status == 200:
        return Outcome.passed("compiled natively (numpy toolchain present)")
    body = r.body or {}
    if r.status == 500 and body.get("error_kind") == "compile" and "unavailable" in str(body.get("error", "")):
        return Outcome.passed("structured 'compile unavailable' contract (edge runtime ships no numpy)")
    return Outcome.failed(f"{r.status}: {_error_of(r)} (expected 200 or a structured compile error)")


def case_run_parse_error(s: Suite) -> Outcome:
    broken = "//@version=6\nindicator('broken'\nplot(close)"
    r = s.request("POST", "/run", {"script": broken, "ohlcv": make_bars(20)})
    if r.status != 500:
        return Outcome.failed(f"expected 500, got {r.status}: {_error_of(r)}")
    if (r.body or {}).get("error_kind") != "parse":
        return Outcome.failed(f"error_kind={(r.body or {}).get('error_kind')!r}")
    return Outcome.passed("parse error → error_kind=parse")


def case_run_timeout(s: Suite) -> Outcome:
    r = s.request("POST", "/run", {"script": SIMPLE, "ohlcv": make_bars(10_000), "timeout_seconds": 0.001})
    s.engine(_engine_ms(r))
    if r.status != 504:
        return Outcome.failed(f"expected 504, got {r.status}")
    body = r.body or {}
    if body.get("error_kind") != "runtime" or "timed out" not in str(body.get("error", "")).lower():
        return Outcome.failed(f"unexpected timeout contract: {json.dumps(body)[:160]}")
    return Outcome.passed("wall-clock budget → 504 timed out · error_kind=runtime")


def case_run_r2_nodata(s: Suite) -> Outcome:
    r = s.request("POST", "/run", {"script": SIMPLE, "symbol": "NODATA42", "timeframe": "1d"})
    s.engine(_engine_ms(r))
    if r.status == 501:
        return Outcome.failed("R2 not configured on the worker")
    if r.status != 200:
        return Outcome.failed(f"{r.status}: {_error_of(r)}")
    body = r.body or {}
    if body.get("bars") != 0 or not body.get("note"):
        return Outcome.failed(f"expected empty-data note, got {json.dumps(body)[:160]}")
    return Outcome.passed("missing R2 data → 200 + hint (no crash)")


def case_r2_ingest(s: Suite) -> Outcome:
    r = s.request(
        "POST",
        "/ingest",
        {"symbol": R2TEST_SYMBOL, "timeframe": R2TEST_TIMEFRAME, "bars": make_bars(R2TEST_BARS)},
    )
    if r.status != 200:
        return Outcome.failed(f"{r.status}: {_error_of(r)}")
    body = r.body or {}
    if body.get("ingested") != R2TEST_BARS:
        return Outcome.failed(f"ingested={body.get('ingested')!r} (expected {R2TEST_BARS})")
    return Outcome.passed(f"{R2TEST_SYMBOL}/{R2TEST_TIMEFRAME} · ingested={body.get('ingested')}")


def case_r2_roundtrip(s: Suite) -> Outcome:
    r = s.request(
        "POST",
        "/run",
        {
            "script": SIMPLE,
            "symbol": R2TEST_SYMBOL,
            "timeframe": R2TEST_TIMEFRAME,
            "max_bars": R2TEST_BARS,
        },
    )
    s.engine(_engine_ms(r))
    if r.status != 200:
        return Outcome.failed(f"{r.status}: {_error_of(r)}")
    bars = (r.body or {}).get("bars")
    if not isinstance(bars, int) or bars < R2TEST_BARS:
        return Outcome.failed(f"bars={bars!r} (expected ≥{R2TEST_BARS} from R2)")
    return Outcome.passed(f"read back {bars} bars from R2 (no inline ohlcv)")


def case_feed_refresh(s: Suite) -> Outcome:
    r = s.request("POST", "/feed/refresh", {"symbol": "BTCUSDT", "timeframe": "1m", "limit": 3})
    if r.status != 200:
        return Outcome.failed(f"{r.status}: {_error_of(r)}")
    feed = (r.body or {}).get("feed") or []
    info = feed[0] if feed and isinstance(feed[0], dict) else {}
    fetched = info.get("fetched", 0)
    if not fetched:
        return Outcome.failed(f"no klines fetched: {json.dumps(info)[:160]}")
    return Outcome.passed(
        f"source={info.get('source')} · fetched={fetched} · ingested={info.get('ingested')}"
    )


def case_scripts_lifecycle(s: Suite) -> Outcome:
    sid = f"live-test-{int(time.time())}"
    steps: list[str] = []
    created = False
    deleted = False
    failure: str | None = None

    def step(label: str, got: int | None, want: int) -> bool:
        nonlocal failure
        steps.append(f"{label}={got}")
        if got != want:
            failure = f"{label}: expected {want}, got {got}"
            return False
        return True

    try:
        r = s.request(
            "POST",
            "/scripts",
            {
                "id": sid,
                "script": SIMPLE,
                "symbol": R2TEST_SYMBOL,
                "timeframe": R2TEST_TIMEFRAME,
                "mode": "interpret",
                "enabled": False,  # never picked up by the cron scheduler
                "max_bars": 500,
            },
        )
        created = r.status == 200
        if not step("deploy", r.status, 200):
            return Outcome.failed(failure or "")

        r = s.request("GET", "/scripts")
        listed = (
            r.status == 200
            and any(isinstance(item, dict) and item.get("id") == sid for item in (r.body or {}).get("scripts") or [])
        )
        steps.append(f"list={'found' if listed else 'missing'}")
        if not step("list-http", r.status, 200) or not listed:
            return Outcome.failed(failure or "deployed script missing from GET /scripts")

        r = s.request("GET", f"/scripts/{sid}")
        if not step("get", r.status, 200):
            return Outcome.failed(failure or "")
        rec = (r.body or {}).get("script") or {}
        if rec.get("script") != SIMPLE:
            return Outcome.failed("GET /scripts/:id did not return the stored source")

        r = s.request("POST", "/run", {"script_id": sid, "ohlcv": make_bars(50)})
        s.engine(_engine_ms(r))
        body = r.body or {}
        if not step("run-by-id-http", r.status, 200) or body.get("deployed_script_id") != sid:
            return Outcome.failed(failure or "script_id run did not resolve to the deployed record")

        r = s.request("DELETE", f"/scripts/{sid}")
        deleted = r.status == 200
        if not step("delete", r.status, 200):
            return Outcome.failed(failure or "")

        r = s.request("GET", f"/scripts/{sid}")
        if not step("verify-gone", r.status, 404):
            return Outcome.failed(failure or "")
    finally:
        if created and not deleted:
            s.request("DELETE", f"/scripts/{sid}")

    return Outcome.passed(f"{sid} · " + " · ".join(steps))


def case_cron_jobs_list(s: Suite) -> Outcome:
    r = s.request("GET", "/cron/jobs")
    if r.status != 200:
        return Outcome.failed(f"{r.status}: {_error_of(r)}")
    jobs = (r.body or {}).get("jobs") or []
    return Outcome.passed(f"jobs={len(jobs)} (read-only)")


def case_cron_run(s: Suite) -> Outcome:
    r = s.request("POST", "/cron/run", {"force": False, "refresh_market": False})
    if r.status == 501:
        body = r.body or {}
        if "error" in body:
            return Outcome.passed("no R2 / nothing to run (501 summary)")
    if r.status != 200:
        return Outcome.failed(f"{r.status}: {_error_of(r)}")
    body = r.body or {}
    results = body.get("results")
    return Outcome.passed(f"results={len(results) if isinstance(results, list) else '?'} · errors={body.get('errors', 0)}")


def case_limits_payload(s: Suite) -> Outcome:
    body = {"script": SIMPLE, "ohlcv": make_bars(2), "pad": "x" * (5 * 1024 * 1024 + 64)}
    r = s.request("POST", "/run", body)
    if r.status != 413:
        return Outcome.failed(f"expected 413 for >5MB body, got {r.status}")
    return Outcome.passed(">5MB body → 413")


# ---------------------------------------------------------------------------
# Report
# ---------------------------------------------------------------------------

_COLOR = sys.stdout.isatty() and os.environ.get("NO_COLOR") is None
_RESET = "\033[0m" if _COLOR else ""


def _c(code: str, text: str) -> str:
    return f"\033[{code}m{text}{_RESET}" if _COLOR else text


def _lat(rep: CaseReport) -> str:
    if not rep.latencies:
        return "—"
    lo = min(rep.latencies)
    mid = statistics.median(rep.latencies)
    hi = max(rep.latencies)
    return f"{lo:.0f}/{mid:.0f}/{hi:.0f}ms"


def _emit(rep: CaseReport) -> None:
    if rep.status == "PASS":
        tag = _c("32", "PASS")
    elif rep.status == "FAIL":
        tag = _c("31", "FAIL")
    else:
        tag = _c("33", "SKIP")
    http = str(rep.http) if rep.http is not None else "—"
    lat = _lat(rep)
    engine = ""
    if rep.engine_ms:
        engine = f" · engine {min(rep.engine_ms):.0f}" + (f"–{max(rep.engine_ms):.0f}" if len(rep.engine_ms) > 1 else "") + "ms"
    detail = rep.detail or ""
    n = f" · n={len(rep.latencies)}" if len(rep.latencies) > 1 else ""
    print(f"  {tag}  {rep.name:<24} {http:>4}  {lat:>18}{n}  {detail}{engine}", flush=True)


def _pctl(values: list[float], q: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    idx = max(0.0, min((len(ordered) - 1) * q, len(ordered) - 1))
    lo, hi = int(math.floor(idx)), int(math.ceil(idx))
    if lo == hi:
        return ordered[lo]
    return ordered[lo] + (ordered[hi] - ordered[lo]) * (idx - lo)


def print_summary(s: Suite, reports: list[CaseReport], started: float) -> None:
    passed = sum(1 for r in reports if r.status == "PASS")
    failed = sum(1 for r in reports if r.status == "FAIL")
    skipped = sum(1 for r in reports if r.status == "SKIP")
    wall = time.perf_counter() - started
    print()
    print("─" * 96)
    line = f"  {passed} passed · {failed} failed · {skipped} skipped · {s.requests} requests · {wall:.1f}s"
    if failed:
        line += "   " + _c("31", "→ fix failures above")
    print(_c("1", line))
    if s.all_latencies:
        print(
            f"  latency (all {len(s.all_latencies)} requests): "
            f"p50 {_pctl(s.all_latencies, 0.5):.0f}ms · "
            f"p95 {_pctl(s.all_latencies, 0.95):.0f}ms · "
            f"max {max(s.all_latencies):.0f}ms"
        )
    engine = [ms for r in reports for ms in r.engine_ms]
    if engine:
        print(f"  engine time (/run cases): min {min(engine):.1f}ms · max {max(engine):.1f}ms (rest is parse/network/edge)")
    if s.client.retries:
        print(_c("33", f"  ↻ {s.client.retries} request(s) retried after 429 — rate limit is 100 req/60s per key"))
    print("─" * 96)


def write_json_report(path: Path, url: str, s: Suite, reports: list[CaseReport], started: float, args: argparse.Namespace) -> None:
    payload = {
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "url": url,
        "worker_version": (s.health or {}).get("version"),
        "engine": (s.health or {}).get("engine"),
        "options": {
            "repeat": args.repeat,
            "r2": not args.no_r2,
            "feed": not args.no_feed,
            "scripts": not args.no_scripts,
            "limits": not args.no_limits,
            "cron": args.with_cron,
        },
        "requests": s.requests,
        "retries": s.client.retries,
        "duration_s": round(time.perf_counter() - started, 2),
        "summary": {
            "pass": sum(1 for r in reports if r.status == "PASS"),
            "fail": sum(1 for r in reports if r.status == "FAIL"),
            "skip": sum(1 for r in reports if r.status == "SKIP"),
        },
        "latency_ms": {
            "p50": round(_pctl(s.all_latencies, 0.5), 1),
            "p95": round(_pctl(s.all_latencies, 0.95), 1),
            "max": round(max(s.all_latencies), 1) if s.all_latencies else None,
        },
        "cases": [
            {
                "name": r.name,
                "status": r.status,
                "http": r.http,
                "detail": r.detail,
                "requests": len(r.latencies),
                "latency_ms": [round(v, 1) for v in r.latencies],
                "engine_ms": [round(v, 1) for v in r.engine_ms],
                "extra": r.extra,
            }
            for r in reports
        ],
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2) + "\n")
    print(f"  report → {path}")


# ---------------------------------------------------------------------------
# Config + main
# ---------------------------------------------------------------------------


def _read_dev_vars(path: Path) -> str | None:
    try:
        text = path.read_text()
    except OSError:
        return None
    for raw in text.splitlines():
        line = raw.strip()
        if line.startswith("#") or "=" not in line:
            continue
        name, _, value = line.partition("=")
        if name.strip() == "API_KEY":
            cleaned = value.strip().strip('"').strip("'")
            if cleaned:
                return cleaned
    return None


def _resolve_api_key(flag: str | None) -> tuple[str | None, str]:
    if flag:
        return flag, "--api-key"
    if os.environ.get("PYNE_API_KEY"):
        return os.environ["PYNE_API_KEY"], "PYNE_API_KEY"
    if os.environ.get("API_KEY"):
        return os.environ["API_KEY"], "API_KEY"
    dev_vars = _read_dev_vars(REPO_ROOT / ".dev.vars")
    if dev_vars:
        return dev_vars, ".dev.vars"
    return None, ""


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Live end-to-end + latency suite for a running pyne-worker",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument("--url", default=os.environ.get("PYNE_WORKER_URL") or DEFAULT_URL, help="Worker base URL")
    parser.add_argument("--api-key", default=None, help="X-API-Key (env PYNE_API_KEY / API_KEY / .dev.vars)")
    parser.add_argument("--repeat", type=int, default=3, help="Latency samples for idempotent cases")
    parser.add_argument("--timeout", type=float, default=40.0, help="Per-request timeout in seconds")
    parser.add_argument("--json", dest="json_path", default=None, help="Write a JSON report to this path")
    parser.add_argument("--no-r2", action="store_true", help="Skip ingest/read-back (no R2 writes)")
    parser.add_argument("--no-feed", action="store_true", help="Skip Bybit feed refresh")
    parser.add_argument("--no-scripts", action="store_true", help="Skip scripts registry lifecycle")
    parser.add_argument("--no-limits", action="store_true", help="Skip the >5MB payload cap case")
    parser.add_argument("--skip-auth-checks", action="store_true", help="Skip the 401 auth cases")
    parser.add_argument("--with-cron", action="store_true", help="Include POST /cron/run (may fire webhooks)")
    parser.add_argument("--quiet", action="store_true", help="Only print failures + summary")
    args = parser.parse_args()

    url = str(args.url)
    if not url.startswith(("http://", "https://")):
        print(f"error: --url must be http(s), got {url!r}", file=sys.stderr)
        return 2
    api_key, key_source = _resolve_api_key(args.api_key)

    print()
    print(_c("1", f"  PYNE-WORKER LIVE TEST — {url}"))
    if api_key:
        print(f"  key: {key_source} · repeat: {args.repeat} · timeout: {args.timeout:.0f}s")
    else:
        print(_c("33", "  no API key found — authenticated cases will be skipped (set PYNE_API_KEY or .dev.vars)"))
    if url.startswith("http://") and "localhost" not in url and "127.0.0.1" not in url:
        print(_c("33", "  warning: plain http to a remote host"))
    print()

    client = Client(url, api_key, args.timeout)
    suite = Suite(client, args.repeat, quiet=args.quiet)
    needs_auth = "no API key (set PYNE_API_KEY)" if not api_key else None

    # Order matters: health first (report header + cold-start sample).
    suite.add("health", case_health, repeat=None)
    suite.add("routing.404", case_404, repeat=1)
    # Auth cases need no configured key — they verify the *absence* of a valid key.
    suite.add("auth.missing_key", case_auth_missing, repeat=1, skip_reason="--skip-auth-checks" if args.skip_auth_checks else None)
    suite.add("auth.wrong_key", case_auth_wrong, repeat=1, skip_reason="--skip-auth-checks" if args.skip_auth_checks else None)
    suite.add("run.interpret", case_run_interpret, repeat=None, skip_reason=needs_auth)
    suite.add("run.strategy_events", case_run_strategy, repeat=1, skip_reason=needs_auth)
    suite.add("run.alerts", case_run_alerts, repeat=1, skip_reason=needs_auth)
    suite.add("run.drawings", case_run_drawings, repeat=1, skip_reason=needs_auth)
    suite.add("run.window", case_run_window, repeat=1, skip_reason=needs_auth)
    suite.add("run.window_full_noop", case_run_window_full, repeat=1, skip_reason=needs_auth)
    suite.add("run.window_validation", case_run_window_validation, repeat=1, skip_reason=needs_auth)
    suite.add("run.libraries", case_run_libraries, repeat=1, skip_reason=needs_auth)
    suite.add("run.inputs", case_run_inputs, repeat=1, skip_reason=needs_auth)
    suite.add("run.mode_auto", case_run_mode_auto, repeat=1, skip_reason=needs_auth)
    suite.add("run.mode_compile", case_run_mode_compile, repeat=1, skip_reason=needs_auth)
    suite.add("run.parse_error", case_run_parse_error, repeat=1, skip_reason=needs_auth)
    suite.add("run.timeout", case_run_timeout, repeat=1, skip_reason=needs_auth)
    suite.add("run.r2_missing_data", case_run_r2_nodata, repeat=1, skip_reason=needs_auth or ("--no-r2" if args.no_r2 else None))
    suite.add("r2.ingest", case_r2_ingest, repeat=1, skip_reason=needs_auth or ("--no-r2" if args.no_r2 else None))
    suite.add("r2.roundtrip", case_r2_roundtrip, repeat=1, skip_reason=needs_auth or ("--no-r2" if args.no_r2 else None))
    suite.add("feed.refresh", case_feed_refresh, repeat=1, skip_reason=needs_auth or ("--no-feed" if args.no_feed else None))
    suite.add("scripts.lifecycle", case_scripts_lifecycle, repeat=1, skip_reason=needs_auth or ("--no-scripts" if args.no_scripts else None))
    suite.add("cron.jobs_list", case_cron_jobs_list, repeat=1, skip_reason=needs_auth)
    suite.add(
        "cron.run",
        case_cron_run,
        repeat=1,
        skip_reason=needs_auth or (None if args.with_cron else "--with-cron not set"),
    )
    suite.add("limits.payload_413", case_limits_payload, repeat=1, skip_reason=needs_auth or ("--no-limits" if args.no_limits else None))

    started = time.perf_counter()
    reports = suite.run()
    print_summary(suite, reports, started)

    if args.json_path:
        write_json_report(Path(args.json_path), url, suite, reports, started, args)

    return 1 if any(r.status == "FAIL" for r in reports) else 0


if __name__ == "__main__":
    raise SystemExit(main())
