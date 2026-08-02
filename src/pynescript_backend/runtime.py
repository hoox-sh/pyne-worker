# Copyright (c) 2026 HOOX · PYNE · jango-blockchained
# SPDX-License-Identifier: AGPL-3.0-or-later

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

import hashlib
import math
import re
import time
import uuid

from typing import Any

from pynescript.ast.helper import parse
from pynescript.ast.helper import walk
from pynescript.util.time_parts import apply_utc_parts_to_context

from pynescript_backend.evaluator import CustomEvaluator
from pynescript_backend.evaluator import _NaValue
from pynescript_backend.series import PineSeries

# Parse tree cache (source sha256 → AST). Bounded.
_PARSE_CACHE: dict[str, Any] = {}
_PARSE_CACHE_MAX = 64

# Host-side compile cache (raw source sha256 → CompiledScript). Aligns with SoT
# backend/runtime.py (R5–R6 Agent 05/12).
_HOST_COMPILE_CACHE: dict[str, Any] = {}
_HOST_COMPILE_CACHE_MAX = 64

# Auto-mode negative cache: source sha256 → compile failure reason.
_HOST_COMPILE_FAIL_CACHE: dict[str, str] = {}
_HOST_COMPILE_FAIL_CACHE_MAX = 128

# Compiler package availability for mode=auto prefilter (None = not probed yet).
# Numba is NOT required for eligibility — object-mode compile is pure-Python.
_HAS_COMPILER: bool | None = None

# Structured Runtime error kinds (surfaced as ``error_kind``; keep string ``error``).
ERROR_KIND_PARSE = "parse"
ERROR_KIND_COMPILE = "compile"
ERROR_KIND_RUNTIME = "runtime"
ERROR_KIND_DATA = "data"
ERROR_KIND_ORDER = "order"
ERROR_KIND_MODE = "mode"

_CAL_NAME_RE = re.compile(
    r"\b(year|month|dayofmonth|hour|minute|second|dayofweek)\b",
)
# fill() needs plot() to return Plot handles (PlotRegistry) on hosts that use it.
_FILL_CALL_RE = re.compile(r"\bfill\s*\(")
# Derived built-in series — skip update/append when script never names them.
_HL2_RE = re.compile(r"\bhl2\b")
_HLC3_RE = re.compile(r"\bhlc3\b")
_OHLC4_RE = re.compile(r"\bohlc4\b")
_HLCC4_RE = re.compile(r"\bhlcc4\b")


def _error_payload(
    message: str,
    *,
    kind: str,
    exc: BaseException | None = None,
    bar_index: int | None = None,
    bar_time: Any = None,
) -> dict[str, Any]:
    """Build a classified Runtime error dict (fail-closed host contract).

    Always includes ``error`` (human message). Adds ``error_kind`` and optional
    ``error_type`` / ``error_bar`` / ``error_bar_time`` for hosts and tests.
    """
    body: dict[str, Any] = {
        "error": message,
        "error_kind": kind,
    }
    if exc is not None:
        body["error_type"] = type(exc).__name__
    if bar_index is not None:
        body["error_bar"] = int(bar_index)
    if bar_time is not None:
        body["error_bar_time"] = bar_time
    return body


def _format_exc_message(prefix: str, exc: BaseException) -> str:
    """``Prefix: TypeName: detail`` (omit redundant type when message already names it)."""
    et = type(exc).__name__
    detail = str(exc).strip() or et
    if detail == et:
        return f"{prefix}: {et}"
    return f"{prefix}: {et}: {detail}"


def _clear_pine_call_sites(tree: Any) -> None:
    """Drop evaluator-bound call-site caches from a shared AST tree.

    ``visit_Call`` stores ``_pine_call_site`` *on the AST node*, including bound
    method handlers from the evaluator that first resolved the site. Hosts
    (and package ``parse``) cache trees by source hash and reuse them across
    ``Runtime`` instances — without this clear, a second run still invokes the
    *first* evaluator's ``plot`` / ``ta.*`` handlers (empty plots / wrong state).

    Safe within a single multi-bar run: clear once at run start; bar 0 rebinds
    for the current evaluator; later bars keep the hot path.
    """
    try:
        for node in walk(tree):
            if getattr(node, "_pine_call_site", None) is not None:
                try:
                    delattr(node, "_pine_call_site")
                except Exception:
                    try:
                        object.__setattr__(node, "_pine_call_site", None)
                    except Exception:
                        pass
    except Exception:
        pass


def _json_safe_number(x: Any) -> float | None:
    """Map NaN/±Inf (and numpy scalars) to ``None`` for strict JSON / browsers."""
    if x is None:
        return None
    try:
        if hasattr(x, "item") and not isinstance(x, (bytes, str, dict, list)):
            x = x.item()
    except Exception:  # noqa: BLE001
        pass
    if isinstance(x, bool):
        return float(x)
    if isinstance(x, (int, float)):
        fx = float(x)
        if math.isnan(fx) or math.isinf(fx):
            return None
        return fx
    return None


def _series_values_jsonable(values: Any) -> list[Any]:
    """Convert a plot series (list / array-like) to JSON-safe list of floats|null.

    CF worker must not hard-require numpy; use pure-Python path with optional
    numpy fast path when present (local/dev with full pynescript stack).
    """
    if values is None:
        return []
    try:
        import numpy as np  # noqa: PLC0415

        if isinstance(values, np.ndarray):
            kind = values.dtype.kind
            if kind in "f":
                arr = np.asarray(values, dtype=np.float64).ravel()
                n = int(arr.size)
                if n == 0:
                    return []
                finite = np.isfinite(arr)
                if bool(finite.all()):
                    return arr.tolist()
                out: list[Any] = arr.tolist()
                bad = np.flatnonzero(~finite)
                for i in bad:
                    out[int(i)] = None
                return out
            if kind in "iu":
                return np.asarray(values, dtype=np.float64).ravel().tolist()
            if kind == "b":
                return [bool(x) for x in values.ravel()]
            values = values.tolist()
    except Exception:
        pass
    if hasattr(values, "tolist") and not isinstance(values, (list, tuple)):
        try:
            values = values.tolist()
        except Exception:
            return []
    if not isinstance(values, (list, tuple)):
        return []
    out_list: list[Any] = []
    append = out_list.append
    for x in values:
        if x is None:
            append(None)
        elif isinstance(x, bool):
            append(x)
        elif isinstance(x, (int, float)):
            fx = float(x)
            if math.isnan(fx) or math.isinf(fx):
                append(None)
            else:
                append(fx)
        elif hasattr(x, "item") and not isinstance(x, (str, bytes)):
            append(_json_safe_number(x))
        else:
            append(x if isinstance(x, (str, dict, list)) else None)
    return out_list


def _parse_script(source_code: str) -> Any:
    key = hashlib.sha256(source_code.encode("utf-8")).hexdigest()
    tree = _PARSE_CACHE.get(key)
    if tree is not None:
        return tree
    tree = parse(source_code, mode="exec")
    if len(_PARSE_CACHE) >= _PARSE_CACHE_MAX:
        try:
            _PARSE_CACHE.pop(next(iter(_PARSE_CACHE)))
        except StopIteration:
            pass
    _PARSE_CACHE[key] = tree
    return tree

def _hl2(bar: dict) -> float:
    return (bar.get("high", 0.0) + bar.get("low", 0.0)) / 2.0


def _hlc3(bar: dict) -> float:
    return (bar.get("high", 0.0) + bar.get("low", 0.0) + bar.get("close", 0.0)) / 3.0


def _tr(bar: dict, prev_close: float | None) -> float | None:
    """True range for current bar (Pine built-in series ``tr``)."""
    h, l = bar.get("high"), bar.get("low")
    if h is None or l is None:
        return None
    try:
        hi, lo = float(h), float(l)
        if prev_close is None:
            return hi - lo
        return max(hi - lo, abs(hi - prev_close), abs(lo - prev_close))
    except (TypeError, ValueError):
        return None


def _ohlc4(bar: dict) -> float:
    return (bar.get("open", 0.0) + bar.get("high", 0.0) + bar.get("low", 0.0) + bar.get("close", 0.0)) / 4.0


def _hlcc4(bar: dict) -> float:
    return (bar.get("high", 0.0) + bar.get("low", 0.0) + bar.get("close", 0.0) + bar.get("close", 0.0)) / 4.0


_REQUIRED_BAR_FIELDS = {"open", "high", "low", "close", "time"}


def _validate_bars(ohlcv_data: list[dict]) -> str | None:
    """Validate OHLCV bar data.

    Returns ``None`` if valid, or an error message string if invalid.
    """
    if not isinstance(ohlcv_data, list):
        return "OHLCV data must be a list"
    for i, bar in enumerate(ohlcv_data):
        if not isinstance(bar, dict):
            return f"Bar at index {i} is not a dict"
        missing = _REQUIRED_BAR_FIELDS - set(bar)
        if missing:
            return f"Bar at index {i} missing fields: {', '.join(sorted(missing))}"
    return None


class Syminfo:
    """Symbol information namespace for Pine Script builtins.

    Contains information about the current symbol like ticker, currency, etc.
    Added in Pine Script v5, with isin and current_contract added in 2025.
    """

    # Basic symbol info (existing)
    tickerid: str = "AAPL"
    currency: str = "USD"
    type: str = "stock"
    session: str = "regular"
    tick_size: float = 0.01
    pointvalue: float = 1.0
    mintick: float = 0.01
    description: str = "Apple Inc."
    strategy_type: str = "long"
    prefix: str = "NASDAQ"
    name: str = "AAPL"

    # November 2025: ISIN (International Securities Identification Number)
    isin: str = ""  # 12-character ISIN code, empty string if not available

    # July 2025: Current contract for continuous futures
    current_contract: str | None = None  # Ticker ID of underlying contract for continuous futures

    # November 2024: Minimum contract size
    mincontract: int = 1


class Chartinfo:
    """Chart information namespace for Pine Script builtins."""

    type: str = "candle"
    aggtype: str = "Standard"
    time: int = 0
    status: str = "regular"


class Timeframe:
    """Timeframe information namespace for Pine Script builtins.

    Attribute names match TradingView Pine: ``isdaily`` / ``ismonthly`` /
    ``isdwm`` (not ``is_daily``). Defaults assume a daily chart (corpus OHLCV).
    """

    period: str = "D"  # e.g., "1D", "1H", "5"
    multiplier: int = 1
    isintraday: bool = False
    isdaily: bool = True
    isweekly: bool = False
    ismonthly: bool = False
    isseconds: bool = False
    isinseconds: bool = False
    isminutes: bool = False
    ishours: bool = False
    isdwm: bool = True  # daily or weekly or monthly
    current: str = "D"

    # November 2024: Main period from chart's main context
    main_period: str = "D"

    # Back-compat aliases (older hosts / tests)
    is_daily: bool = True
    is_weekly: bool = False
    is_monthly: bool = False
    is_seconds: bool = False

    @classmethod
    def from_bar_spacing(cls, ohlcv_data: list[dict]) -> Timeframe:
        """Infer timeframe flags from median bar spacing (ms)."""
        tf = cls()
        if len(ohlcv_data) < 2:
            return tf
        deltas: list[int] = []
        for i in range(1, min(len(ohlcv_data), 50)):
            t0 = ohlcv_data[i - 1].get("time")
            t1 = ohlcv_data[i].get("time")
            if t0 is None or t1 is None:
                continue
            try:
                d = int(t1) - int(t0)
            except (TypeError, ValueError):
                continue
            if d > 0:
                deltas.append(d)
        if not deltas:
            return tf
        deltas.sort()
        med = deltas[len(deltas) // 2]
        minute = 60_000
        hour = 60 * minute
        day = 24 * hour
        week = 7 * day
        if med < day * 0.9:
            # Intraday
            tf.isdaily = False
            tf.is_daily = False
            tf.isdwm = False
            tf.isintraday = True
            if med < minute * 1.5:
                # sub-minute / seconds
                secs = max(1, round(med / 1000))
                tf.period = f"{secs}S"
                tf.current = tf.period
                tf.main_period = tf.period
                tf.multiplier = secs
                tf.isseconds = True
                tf.isinseconds = True
                tf.is_seconds = True
            elif med < hour * 0.9:
                mins = max(1, round(med / minute))
                tf.period = str(mins) if mins != 1 else "1"
                tf.current = tf.period
                tf.main_period = tf.period
                tf.multiplier = mins
                tf.isminutes = True
            else:
                hrs = max(1, round(med / hour))
                tf.period = f"{hrs}H" if hrs != 1 else "60"
                tf.current = tf.period
                tf.main_period = tf.period
                tf.multiplier = hrs
                tf.ishours = True
                tf.isminutes = True  # hours are still intraday minutes TF family
        elif med < week * 0.9:
            tf.period = "D"
            tf.current = "D"
            tf.main_period = "D"
            tf.multiplier = 1
            tf.isdaily = True
            tf.is_daily = True
            tf.isdwm = True
            tf.isintraday = False
        else:
            # Weekly or coarser — treat as weekly
            tf.period = "W"
            tf.current = "W"
            tf.main_period = "W"
            tf.multiplier = 1
            tf.isdaily = False
            tf.is_daily = False
            tf.isweekly = True
            tf.is_weekly = True
            tf.isdwm = True
            tf.isintraday = False
        return tf


class Barstate:
    """Bar state information namespace for Pine Script builtins."""

    isfirst: bool = False
    islast: bool = False
    isnew: bool = True
    ishistory: bool = True
    isconfirmed: bool = True
    islastconfirmedhistory: bool = False
    isrealtime: bool = False
    iscomposite: bool = False


class Chart:
    """Chart namespace for Pine Script builtins."""

    fg_color: str = "#000000"
    bg_color: str = "#FFFFFF"
    resolution: str = "D"

    # Chart display mode
    is_heikin_ashi: bool = False
    is_kagi: bool = False
    is_line_break: bool = False
    is_point_figure: bool = False
    is_renko: bool = False
    is_range: bool = False


class Runtime:
    def __init__(self, symbol: str = "AAPL", run_id: str | None = None):
        """
        Initialize the runtime with optional symbol configuration.

        Args:
            symbol: The symbol to use for the runtime (default: "AAPL")
            run_id: Optional unique run identifier. Generated if not provided.
        """
        self.symbol = symbol
        self._run_id = run_id or uuid.uuid4().hex[:16]
        self._syminfo = Syminfo()
        self._syminfo.tickerid = symbol
        self._syminfo.name = symbol
        self._syminfo.prefix = self._extract_prefix(symbol)

        # February 2025: bid/ask variables (only available on 1T timeframe)
        self._bid: float | None = None
        self._ask: float | None = None

        # November 2024: main ticker reference
        self._main_tickerid: str = symbol

    def _extract_prefix(self, symbol: str) -> str:
        """Extract prefix from symbol (e.g., 'NASDAQ' from 'NASDAQ:AAPL')."""
        if ":" in symbol:
            return symbol.split(":", maxsplit=1)[0]
        return ""

    def configure_footprint(self, footprint_data: dict) -> None:
        """Configure syminfo based on footprint data.

        Args:
            footprint_data: Dictionary containing footprint configuration
        """
        if "isin" in footprint_data:
            self._syminfo.isin = footprint_data["isin"]
        if "current_contract" in footprint_data:
            self._syminfo.current_contract = footprint_data["current_contract"]

    def update_bid_ask(self, bid: float | None, ask: float | None) -> None:
        """Update bid/ask prices (February 2025 feature).

        Args:
            bid: Bid price (highest buy order)
            ask: Ask price (lowest sell order)
        """
        self._bid = bid
        self._ask = ask

    def run(
        self,
        source_code: str,
        ohlcv_data: list[dict],
        timeout_seconds: float | None = None,
        mode: str = "interpret",
        inputs: dict | None = None,
    ) -> dict[str, Any]:
        """Execute the script over the provided OHLCV data.

        Args:
            source_code: Pine Script source to run.
            ohlcv_data: List of dicts with 'open', 'high', 'low', 'close', 'time'.
            timeout_seconds: Maximum wall-clock time for execution. When
                exceeded, execution stops early and returns a partial result
                with a ``timed_out`` flag. ``None`` means no timeout.
            mode: ``interpret`` (default), ``compile`` (Numba path), or
                ``auto`` (try compile, fall back to interpret).
            inputs: Optional Pine ``input.*`` overrides (title → value). Applied
                on interpret. Non-empty inputs force ``mode=auto`` onto interpret
                (compile cannot apply overrides).

        Returns:
            dict with ``plots``, ``events``, ``alerts``, ``inputs``, ``count``,
            ``script_id``, ``run_id``, or ``error`` / ``error_kind`` on failure.
        """
        # Validate bars first
        bar_err = _validate_bars(ohlcv_data)
        if bar_err:
            return _error_payload(bar_err, kind=ERROR_KIND_DATA)

        mode_norm = (mode or "interpret").strip().lower()
        if mode_norm == "compile":
            return self._run_compiled(source_code, ohlcv_data)
        if mode_norm == "auto":
            return self._run_auto(
                source_code,
                ohlcv_data,
                timeout_seconds=timeout_seconds,
                inputs=inputs,
            )
        if mode_norm not in ("interpret",):
            return _error_payload(
                f"Unknown mode: {mode!r} (use interpret|compile|auto)",
                kind=ERROR_KIND_MODE,
            )

        # Parse once (cached by source hash for multi-run / warm hosts)
        try:
            tree = _parse_script(source_code)
        except SyntaxError as e:
            return _error_payload(
                f"Syntax Error: {e!s}",
                kind=ERROR_KIND_PARSE,
                exc=e,
            )
        except Exception as e:
            return _error_payload(
                f"Parse Error: {e!s}",
                kind=ERROR_KIND_PARSE,
                exc=e,
            )
        # Shared parse trees carry bound call-site handlers from prior evaluators.
        # Clear once per run so this evaluator rebinds (cross-bar sites stay hot).
        _clear_pine_call_sites(tree)

        # Initialize Series
        open_series = PineSeries()
        high_series = PineSeries()
        low_series = PineSeries()
        close_series = PineSeries()
        volume_series = PineSeries()
        hl2_series = PineSeries()
        hlc3_series = PineSeries()
        ohlc4_series = PineSeries()
        hlcc4_series = PineSeries()
        tr_series = PineSeries()
        # ``time`` / ``time_close`` must be series so ``time[n]`` works
        # (e.g. chart.point.from_time(time[length], price) in pivot scripts).
        time_series = PineSeries()
        time_close_series = PineSeries()

        # Series lists for builtin technical indicators
        _series_lists: dict[str, list[Any]] = {
            "open": [],
            "high": [],
            "low": [],
            "close": [],
            "volume": [],
            "hl2": [],
            "hlc3": [],
            "ohlc4": [],
            "hlcc4": [],
            "tr": [],
            "time": [],
            "time_close": [],
        }

        # Infer timeframe from bar spacing (daily corpus → isdwm; 1H → intraday)
        tf = Timeframe.from_bar_spacing(ohlcv_data)
        barstate = Barstate()
        context = {
            "open": open_series,
            "high": high_series,
            "low": low_series,
            "close": close_series,
            "volume": volume_series,
            "hl2": hl2_series,
            "hlc3": hlc3_series,
            "ohlc4": ohlc4_series,
            "hlcc4": hlcc4_series,
            "tr": tr_series,
            "na": _NaValue(),
            "NaN": None,
            # Symbol info namespace (November 2025: syminfo.isin, July 2025: syminfo.current_contract)
            "syminfo": self._syminfo,
            "timeframe": tf,
            "barstate": barstate,
            "chart": Chart(),
            # Flat timeframe.* keys survive local vars that shadow ``timeframe``
            "timeframe.period": tf.period,
            "timeframe.main_period": tf.main_period,
            "timeframe.multiplier": tf.multiplier,
            "timeframe.isintraday": tf.isintraday,
            "timeframe.isdaily": tf.isdaily,
            "timeframe.isweekly": tf.isweekly,
            "timeframe.ismonthly": tf.ismonthly,
            "timeframe.isseconds": tf.isseconds,
            "timeframe.isinseconds": tf.isinseconds,
            "timeframe.isminutes": tf.isminutes,
            "timeframe.ishours": tf.ishours,
            "timeframe.isdwm": tf.isdwm,
            # Per-bar counters / series updated in the loop below
            "bar_index": 0,
            "time": time_series,
            "time_close": time_close_series,
            "last_bar_index": max(0, len(ohlcv_data) - 1),
            "last_bar_time": ohlcv_data[-1].get("time", 0) if ohlcv_data else 0,
        }

        evaluator = CustomEvaluator(context=context)
        evaluator._var_declarations.clear()
        evaluator.current_series = _series_lists
        # Host UI overrides for input.* (keyed by title) — SoT parity
        if inputs and isinstance(inputs, dict):
            try:
                evaluator._input_overrides = dict(inputs)  # type: ignore[attr-defined]
            except Exception:
                pass
        try:
            evaluator._input_declarations = []  # type: ignore[attr-defined]
        except Exception:
            pass
        # fill() needs plot handles on hosts with PlotRegistry; worker plot is
        # scalar-only but keep the flag for shared evaluator/package paths.
        try:
            evaluator._pine_need_plot_ids = bool(_FILL_CALL_RE.search(source_code))  # type: ignore[attr-defined]
        except Exception:
            pass

        # Fresh drawing registries so leftover labels/lines from prior runs
        # do not leak into this response (DrawingRegistry is process-global).
        try:
            from pynescript.ast.evaluator.builtins.drawing import DrawingRegistry

            DrawingRegistry.reset()
        except Exception:
            pass

        # Alert engine: clear per-run so prior jobs do not leak firings
        clear_alerts = getattr(evaluator, "clear_alerts", None)
        if callable(clear_alerts):
            try:
                clear_alerts()
            except Exception:
                pass

        # Per-bar first-plot values (worker response keeps simple plots list)
        plot0_values: list[Any] = []
        plot0_append = plot0_values.append
        all_events: list[dict] = []
        all_events_append = all_events.append

        # Generate stable script_id from source hash
        script_id = hashlib.sha256(source_code.encode("utf-8")).hexdigest()[:16]
        run_id = self._run_id

        # Wall-clock deadline for circuit breaker
        timed_out = False
        if timeout_seconds is not None:
            deadline = time.monotonic() + timeout_seconds
        else:
            deadline = None

        n_bars = len(ohlcv_data)
        last_bar_i = n_bars - 1
        # Pre-extract columns once
        col_open = [b.get("open") for b in ohlcv_data]
        col_high = [b.get("high") for b in ohlcv_data]
        col_low = [b.get("low") for b in ohlcv_data]
        col_close = [b.get("close") for b in ohlcv_data]
        col_vol = [b.get("volume") for b in ohlcv_data]
        col_time = [b.get("time", 0) or 0 for b in ohlcv_data]
        need_calendar = bool(_CAL_NAME_RE.search(source_code))
        need_hl2 = bool(_HL2_RE.search(source_code))
        need_hlc3 = bool(_HLC3_RE.search(source_code))
        need_ohlc4 = bool(_OHLC4_RE.search(source_code))
        need_hlcc4 = bool(_HLCC4_RE.search(source_code))
        has_bid_ask = any(("bid" in b) or ("ask" in b) for b in ohlcv_data)

        # Pre-bind series list locals + in-place cap (mirrors backend/runtime.py)
        sl_open = _series_lists["open"]
        sl_high = _series_lists["high"]
        sl_low = _series_lists["low"]
        sl_close = _series_lists["close"]
        sl_vol = _series_lists["volume"]
        sl_hl2 = _series_lists["hl2"]
        sl_hlc3 = _series_lists["hlc3"]
        sl_ohlc4 = _series_lists["ohlc4"]
        sl_hlcc4 = _series_lists["hlcc4"]
        sl_tr = _series_lists["tr"]
        sl_time = _series_lists["time"]
        sl_time_close = _series_lists["time_close"]
        _series_list_refs_list = [
            sl_open,
            sl_high,
            sl_low,
            sl_close,
            sl_vol,
            sl_tr,
            sl_time,
            sl_time_close,
        ]
        if need_hl2:
            _series_list_refs_list.append(sl_hl2)
        if need_hlc3:
            _series_list_refs_list.append(sl_hlc3)
        if need_ohlc4:
            _series_list_refs_list.append(sl_ohlc4)
        if need_hlcc4:
            _series_list_refs_list.append(sl_hlcc4)
        _series_list_refs = tuple(_series_list_refs_list)
        series_cap = int(getattr(evaluator, "_SERIES_MAX", 256) or 256)
        series_cap_limit = series_cap + 64

        open_update = open_series.update
        high_update = high_series.update
        low_update = low_series.update
        close_update = close_series.update
        volume_update = volume_series.update
        hl2_update = hl2_series.update
        hlc3_update = hlc3_series.update
        ohlc4_update = ohlc4_series.update
        hlcc4_update = hlcc4_series.update
        tr_update = tr_series.update
        time_update = time_series.update
        time_close_update = time_close_series.update

        # Static historical barstate flags (do not change mid-run)
        barstate.isnew = True
        barstate.ishistory = True
        barstate.isconfirmed = True
        barstate.isrealtime = False

        visit = evaluator.visit
        reset_plots = evaluator.reset_plots
        plot_outputs = evaluator.plot_outputs
        strategy_state = getattr(evaluator, "_strategy_state", None)
        strategy_events = getattr(strategy_state, "_events", None) if strategy_state else None
        set_defs_locked = True
        prev_close_f: float | None = None

        for bar_index in range(n_bars):
            # Wall-clock check every 32 bars (still first bar + last via natural end)
            if deadline is not None and (bar_index & 31) == 0 and time.monotonic() > deadline:
                timed_out = True
                break
            o = col_open[bar_index]
            h = col_high[bar_index]
            l = col_low[bar_index]
            c = col_close[bar_index]
            v = col_vol[bar_index]
            # Always compute derived OHLC so input.source overrides can select them
            # even when the script body never mentions hl2/hlc3/ohlc4.
            try:
                of = float(o)
                hf = float(h)
                lf = float(l)
                cf = float(c)
                hl2_val = (hf + lf) * 0.5
                hlc3_val = (hf + lf + cf) / 3.0
                ohlc4_val = (of + hf + lf + cf) * 0.25
                hlcc4_val = (hf + lf + cf + cf) * 0.25 if need_hlcc4 else None
                if prev_close_f is None:
                    tr_val: float | None = hf - lf
                else:
                    tr_val = max(hf - lf, abs(hf - prev_close_f), abs(lf - prev_close_f))
                prev_close_f = cf
            except (TypeError, ValueError):
                bar = ohlcv_data[bar_index]
                hl2_val = _hl2(bar)
                hlc3_val = _hlc3(bar)
                ohlc4_val = _ohlc4(bar)
                hlcc4_val = _hlcc4(bar) if need_hlcc4 else None
                try:
                    prev_c = prev_close_f
                    tr_val = _tr(bar, prev_c)
                    prev_close_f = float(c)
                except (TypeError, ValueError):
                    tr_val = None
                    prev_close_f = None

            open_update(o)
            high_update(h)
            low_update(l)
            close_update(c)
            volume_update(v)
            hl2_update(hl2_val)
            hlc3_update(hlc3_val)
            ohlc4_update(ohlc4_val)
            if need_hl2:
                sl_hl2.append(hl2_val)
            if need_hlc3:
                sl_hlc3.append(hlc3_val)
            if need_ohlc4:
                sl_ohlc4.append(ohlc4_val)
            if need_hlcc4:
                hlcc4_update(hlcc4_val)
                sl_hlcc4.append(hlcc4_val)
            tr_update(tr_val)

            # Append-only + in-place series cap (pre-bound list refs stay valid)
            sl_open.append(o)
            sl_high.append(h)
            sl_low.append(l)
            sl_close.append(c)
            sl_vol.append(v)
            sl_tr.append(tr_val)

            bar_time = col_time[bar_index]
            if bar_index < last_bar_i:
                time_close = col_time[bar_index + 1] or bar_time
            else:
                if bar_index > 0:
                    prev_t = col_time[bar_index - 1] or bar_time
                    spacing = max(1, int(bar_time) - int(prev_t))
                else:
                    spacing = 86_400_000
                time_close = int(bar_time) + spacing
            context["bar_index"] = bar_index
            time_update(bar_time)
            time_close_update(time_close)
            sl_time.append(bar_time)
            sl_time_close.append(time_close)

            # Cap after all series for this bar are appended (aligned lists)
            n_hist = len(sl_close)
            if n_hist > series_cap_limit:
                drop = n_hist - series_cap
                for _lst in _series_list_refs:
                    del _lst[:drop]

            if need_calendar:
                apply_utc_parts_to_context(context, bar_time)

            is_last = bar_index == last_bar_i
            barstate.isfirst = bar_index == 0
            barstate.islast = is_last
            barstate.islastconfirmedhistory = is_last

            if has_bid_ask:
                bar = ohlcv_data[bar_index]
                if "bid" in bar:
                    self._bid = bar["bid"]
                if "ask" in bar:
                    self._ask = bar["ask"]

            reset_plots()
            if strategy_events:
                strategy_events.clear()
            evaluator._cross_call_i = 0  # type: ignore[attr-defined]
            evaluator._ta_call_i = 0  # type: ignore[attr-defined]
            evaluator._plot_call_i = 0  # type: ignore[attr-defined]

            try:
                visit(tree)
            except (SyntaxError, TypeError, ValueError, ZeroDivisionError, AttributeError, IndexError, RuntimeError, OverflowError) as e:
                return _error_payload(
                    f"Runtime Error at bar {bar_index} (time={bar_time}): {e!s}",
                    kind=ERROR_KIND_RUNTIME,
                    exc=e,
                    bar_index=bar_index,
                    bar_time=bar_time,
                )

            if set_defs_locked:
                evaluator._pine_defs_locked = True  # type: ignore[attr-defined]
                set_defs_locked = False

            if strategy_state is not None and strategy_events:
                for ev in strategy_state.drain_events():
                    ev_dict = ev.to_dict()
                    # Worker hosts ``time`` as PineSeries for ``time[n]``; some
                    # strategy paths still put the series object into bar_time.
                    bt = ev_dict.get("bar_time")
                    if bt is not None and not isinstance(bt, (int, float, str, bool)):
                        cur = getattr(bt, "current", None)
                        if cur is not None:
                            try:
                                ev_dict["bar_time"] = int(cur)
                            except (TypeError, ValueError):
                                ev_dict["bar_time"] = cur
                        else:
                            try:
                                ev_dict["bar_time"] = int(bt)  # type: ignore[arg-type]
                            except (TypeError, ValueError):
                                pass
                    ev_dict["script_id"] = script_id
                    ev_dict["run_id"] = run_id
                    all_events_append(ev_dict)

            # First plot value only (worker API). plot_outputs holds scalars
            # (or legacy dicts for mixed evaluator versions).
            if plot_outputs:
                p0 = plot_outputs[0]
                plot0_append(p0.get("value") if isinstance(p0, dict) else p0)
            else:
                plot0_append(None)

        drawings: list[Any] = []
        drawing_limits: dict[str, int] = {}
        try:
            from pynescript.ast.evaluator.builtins.drawing import DrawingRegistry

            bar_times = [b.get("time", 0) for b in ohlcv_data]
            if not DrawingRegistry.is_empty():
                drawings = DrawingRegistry.export_for_api(bar_times)
            drawing_limits = DrawingRegistry.limits_dict()
        except Exception:
            drawings = []
            drawing_limits = {}

        # Alert engine export (alert() + true alertcondition firings)
        alerts: list[dict[str, Any]] = []
        alert_conditions: list[dict[str, Any]] = []
        try:
            try:
                from pynescript.ast.evaluator.builtins.alerts import (
                    export_alerts_from_evaluator,
                )

                alerts = list(export_alerts_from_evaluator(evaluator) or [])
            except ImportError:
                # Older vendored pynescript without export helper
                raw = getattr(evaluator, "get_triggered_alerts", None)
                items = raw() if callable(raw) else getattr(evaluator, "_triggered_alerts", None) or []
                for a in items or []:
                    if hasattr(a, "to_dict"):
                        alerts.append(a.to_dict())
                    elif isinstance(a, dict):
                        alerts.append(dict(a))
                    else:
                        t = getattr(a, "time", None)
                        cur = getattr(t, "current", None)
                        if cur is not None and not isinstance(t, (int, float)):
                            t = cur
                        try:
                            t_int = int(t) if t is not None else None
                        except (TypeError, ValueError):
                            t_int = None
                        alerts.append(
                            {
                                "message": str(getattr(a, "message", a)),
                                "freq": str(getattr(a, "freq", "once_per_bar")),
                                "bar_index": getattr(a, "bar_index", None),
                                "time": t_int,
                                "source": getattr(a, "source", "alert"),
                            }
                        )
            exp_c = getattr(evaluator, "export_alert_conditions", None)
            if callable(exp_c):
                alert_conditions = list(exp_c() or [])
            else:
                for c in getattr(evaluator, "_alert_conditions", None) or []:
                    if hasattr(c, "to_dict"):
                        alert_conditions.append(c.to_dict())
                    elif isinstance(c, dict):
                        alert_conditions.append(dict(c))
                    else:
                        alert_conditions.append(
                            {
                                "condition": bool(getattr(c, "condition", False)),
                                "title": str(getattr(c, "title", "Alert")),
                                "message": str(getattr(c, "message", "Alert")),
                                "bar_index": getattr(c, "bar_index", None),
                                "time": getattr(c, "time", None),
                            }
                        )
        except Exception:
            alerts = []
            alert_conditions = []
        for a in alerts:
            if isinstance(a, dict):
                a.setdefault("script_id", script_id)
                a.setdefault("run_id", run_id)

        # Export input.* declarations (AXIS/Script Settings parity with SoT)
        input_defs: list[dict[str, Any]] = []
        try:
            decls = list(getattr(evaluator, "_input_declarations", None) or [])
            seen_titles: set[str] = set()
            for d in decls:
                if not isinstance(d, dict):
                    continue
                t = str(d.get("title") or "")
                if t and t in seen_titles:
                    continue
                if t:
                    seen_titles.add(t)
                safe: dict[str, Any] = {}
                for k, v in d.items():
                    if isinstance(v, (str, int, float, bool)) or v is None:
                        safe[k] = v
                    elif isinstance(v, (list, tuple)):
                        safe[k] = [
                            str(x)
                            if not isinstance(x, (str, int, float, bool, type(None)))
                            else x
                            for x in v
                        ]
                    else:
                        safe[k] = str(v)
                input_defs.append(safe)
        except Exception:
            input_defs = []

        result: dict[str, Any] = {
            "plots": plot0_values,
            "events": all_events,
            "drawings": drawings,
            "alerts": alerts,
            "inputs": input_defs,
            "count": len(plot0_values),
            "script_id": script_id,
            "run_id": run_id,
            "mode": "interpret",
        }
        if alert_conditions:
            result["alert_conditions"] = alert_conditions
        meta_out: dict[str, Any] = {}
        if drawing_limits:
            meta_out.update(drawing_limits)
        if input_defs:
            meta_out["inputs"] = input_defs
        if meta_out:
            result["meta"] = meta_out
        if timed_out:
            result["timed_out"] = True
            result["error"] = "Script execution timed out"
        return result

    @staticmethod
    def _compile_eligible(source_code: str) -> tuple[bool, str]:
        """Return whether compile mode may succeed.

        Numba is only required for pure-numeric (non-object) scripts. Object-mode
        compile works without Numba, so we do not gate eligibility on it here —
        ``compile_script`` raises a clear error when Numba is required but missing.
        """
        global _HAS_COMPILER
        if _HAS_COMPILER is False:
            return False, "compiler package unavailable"
        if _HAS_COMPILER is None:
            try:
                import pynescript.compiler.engine  # noqa: F401

                _HAS_COMPILER = True
            except ImportError:
                _HAS_COMPILER = False
                return False, "compiler package unavailable"
        src = source_code or ""
        if re.search(r"(?m)^\s*import\s+\S+", src):
            return False, "import statements not supported in compile path"
        if "request." in src:
            return False, "request.* not supported in compile path"
        return True, ""

    @staticmethod
    def _source_cache_key(source_code: str) -> str:
        return hashlib.sha256((source_code or "").encode("utf-8")).hexdigest()

    @staticmethod
    def _is_cacheable_compile_failure(err: str) -> bool:
        """True for deterministic source/environment failures (not bar-data runtime)."""
        e = (err or "").strip()
        if not e:
            return False
        if e.startswith("Compiled Runtime Error"):
            return False
        if e.startswith("Compile Error"):
            return True
        if e.startswith("Compile mode unavailable"):
            return True
        low = e.lower()
        if "numba" in low and ("required" in low or "not installed" in low):
            return True
        return False

    @staticmethod
    def _remember_compile_failure(cache_key: str, reason: str) -> None:
        if not cache_key or not reason:
            return
        if (
            len(_HOST_COMPILE_FAIL_CACHE) >= _HOST_COMPILE_FAIL_CACHE_MAX
            and cache_key not in _HOST_COMPILE_FAIL_CACHE
        ):
            try:
                _HOST_COMPILE_FAIL_CACHE.pop(next(iter(_HOST_COMPILE_FAIL_CACHE)))
            except StopIteration:
                pass
        _HOST_COMPILE_FAIL_CACHE[cache_key] = reason

    def _run_auto(
        self,
        source_code: str,
        ohlcv_data: list[dict],
        timeout_seconds: float | None = None,
        inputs: dict | None = None,
    ) -> dict[str, Any]:
        """Try compile; fall back to interpret. Sets ``auto_backend`` + fallback reason."""
        # Compile path does not apply input.* overrides — prefer full host semantics.
        if inputs:
            result = self.run(
                source_code,
                ohlcv_data,
                timeout_seconds=timeout_seconds,
                mode="interpret",
                inputs=inputs,
            )
            if isinstance(result, dict):
                result["mode"] = result.get("mode") or "interpret"
                result["auto_backend"] = "interpret"
                result["compile_fallback_reason"] = "input.* overrides require interpret path"
            return result

        src_key = self._source_cache_key(source_code)
        prior_fail = _HOST_COMPILE_FAIL_CACHE.get(src_key)
        if prior_fail is not None:
            eligible, reason = False, prior_fail
        else:
            eligible, reason = self._compile_eligible(source_code)

        compile_err: str | None = reason or None
        if eligible:
            compiled_result = self._run_compiled(source_code, ohlcv_data)
            if "error" not in compiled_result:
                _HOST_COMPILE_FAIL_CACHE.pop(src_key, None)
                compiled_result["mode"] = compiled_result.get("mode") or "compile"
                compiled_result["auto_backend"] = "compile"
                return compiled_result
            compile_err = str(compiled_result.get("error") or "compile failed")
            if self._is_cacheable_compile_failure(compile_err):
                self._remember_compile_failure(src_key, compile_err)
        elif reason and self._is_cacheable_compile_failure(reason):
            self._remember_compile_failure(src_key, reason)

        result = self.run(
            source_code,
            ohlcv_data,
            timeout_seconds=timeout_seconds,
            mode="interpret",
            inputs=inputs,
        )
        if isinstance(result, dict):
            result["mode"] = result.get("mode") or "interpret"
            result["auto_backend"] = "interpret"
            if compile_err:
                result["compile_fallback_reason"] = compile_err
        return result

    def _run_compiled(self, source_code: str, ohlcv_data: list[dict]) -> dict[str, Any]:
        """Numba/object compile path via pynescript.compiler.

        Pure-numeric scripts need Numba; object-mode (UDT/map/drawing) does not.
        Host caches successful ``CompiledScript`` values by raw-source sha256.
        """
        try:
            from pynescript.compiler.engine import compile_script
        except ImportError as e:
            return _error_payload(
                _format_exc_message("Compile mode unavailable", e),
                kind=ERROR_KIND_COMPILE,
                exc=e,
            )
        if not ohlcv_data:
            return {"plots": [], "events": [], "count": 0, "mode": "compile", "series": {}}

        cache_key = self._source_cache_key(source_code)
        script_id = cache_key[:16]
        compiled = _HOST_COMPILE_CACHE.get(cache_key)
        was_cached = compiled is not None
        if compiled is None:
            try:
                compiled = compile_script(source_code)
            except Exception as e:
                return _error_payload(
                    _format_exc_message("Compile Error", e),
                    kind=ERROR_KIND_COMPILE,
                    exc=e,
                )
            if len(_HOST_COMPILE_CACHE) >= _HOST_COMPILE_CACHE_MAX:
                try:
                    _HOST_COMPILE_CACHE.pop(next(iter(_HOST_COMPILE_CACHE)))
                except StopIteration:
                    pass
            _HOST_COMPILE_CACHE[cache_key] = compiled
            _HOST_COMPILE_FAIL_CACHE.pop(cache_key, None)

        # List pack is CF-safe (no hard numpy dep). Optional numpy single-pass
        # pack is a local/dev optim when available — engine accepts either.
        try:
            import numpy as np  # noqa: PLC0415

            n = len(ohlcv_data)
            opens_a = np.empty(n, dtype=np.float64)
            highs_a = np.empty(n, dtype=np.float64)
            lows_a = np.empty(n, dtype=np.float64)
            closes_a = np.empty(n, dtype=np.float64)
            volumes_a = np.empty(n, dtype=np.float64)
            for i, b in enumerate(ohlcv_data):
                opens_a[i] = float(b.get("open", 0.0))
                highs_a[i] = float(b.get("high", 0.0))
                lows_a[i] = float(b.get("low", 0.0))
                closes_a[i] = float(b.get("close", 0.0))
                volumes_a[i] = float(b.get("volume", 1.0))
            opens, highs, lows, closes, volumes = opens_a, highs_a, lows_a, closes_a, volumes_a
        except Exception:
            opens = [float(b.get("open", 0.0)) for b in ohlcv_data]
            highs = [float(b.get("high", 0.0)) for b in ohlcv_data]
            lows = [float(b.get("low", 0.0)) for b in ohlcv_data]
            closes = [float(b.get("close", 0.0)) for b in ohlcv_data]
            volumes = [float(b.get("volume", 1.0)) for b in ohlcv_data]
        try:
            series_map = compiled.run(opens, highs, lows, closes, volumes)
        except Exception as e:
            return _error_payload(
                _format_exc_message("Compiled Runtime Error", e),
                kind=ERROR_KIND_RUNTIME,
                exc=e,
            )
        if not isinstance(series_map, dict):
            series_map = {}
        # Prefer .get so shared maps are not mutated (SoT host hygiene)
        drawings = series_map.get("__drawings", []) or []
        events = series_map.get("__events", []) or []

        # JSON-safe series map (NaN → null) — multi-plot SoT parity
        json_series: dict[str, list[Any]] = {}
        _to_json = _series_values_jsonable
        for k, v in series_map.items():
            ks = k if isinstance(k, str) else str(k)
            if ks.startswith("__"):
                continue
            json_series[ks] = _to_json(v)

        # Compile-path GC: trim __drawings by declaration caps (defaults 50)
        drawing_limits: dict[str, int] = {
            "max_lines_count": 50,
            "max_labels_count": 50,
            "max_boxes_count": 50,
            "max_polylines_count": 50,
        }
        try:
            from pynescript.ast.evaluator.builtins.drawing import DrawingRegistry

            _hard = {
                "max_lines_count": 500,
                "max_labels_count": 500,
                "max_boxes_count": 500,
                "max_polylines_count": 100,
            }
            for _key, _cap in _hard.items():
                _m = re.search(rf"\b{_key}\s*=\s*(\d+)", source_code or "")
                if _m:
                    try:
                        _n = int(_m.group(1))
                        drawing_limits[_key] = max(1, min(_cap, _n))
                    except (TypeError, ValueError):
                        pass
            if isinstance(drawings, list) and drawings:
                drawings = DrawingRegistry.gc_exported_drawings(drawings, drawing_limits)
        except Exception:
            pass

        final_series: list[Any] = next(iter(json_series.values()), []) if json_series else []
        if isinstance(events, list):
            for ev in events:
                if isinstance(ev, dict):
                    ev.setdefault("script_id", script_id)
                    ev.setdefault("run_id", self._run_id)
        # Compile path does not execute interpret-only alert() side effects yet
        return {
            "plots": final_series,
            "series": json_series,
            "drawings": drawings if isinstance(drawings, list) else [],
            "events": events if isinstance(events, list) else [],
            "alerts": [],
            "count": len(ohlcv_data),
            "script_id": script_id,
            "run_id": self._run_id,
            "mode": "compile",
            "object_mode": getattr(compiled, "object_mode", False),
            "meta": dict(drawing_limits),
            "compile_cached": was_cached,
        }
