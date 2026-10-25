# Copyright (C) 2024-2026 jango_blockchained
#
# This file is part of pynescript.
#
# pynescript is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# pynescript is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU Affero General Public License for more details.
#
# You should have received a copy of the GNU Affero General Public License
# along with pynescript.  If not, see <https://www.gnu.org/licenses/>.
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""JSON-safe envelopes and series summaries for MCP tool results."""

from __future__ import annotations

import math

from typing import Any


SAMPLE_DEFAULT = 8
SAMPLE_MAX = 64
BARS_MAX = 5000
TIMEOUT_DEFAULT = 30.0
TIMEOUT_MAX = 120.0

_SECRET_KEYS = frozenset(
    {
        "api_key",
        "apikey",
        "secret",
        "token",
        "admin_token",
        "admintoken",
        "webhook",
        "webhook_url",
        "authorization",
        "password",
    }
)


def ok(**fields: Any) -> dict[str, Any]:
    """Successful tool payload."""
    return {"ok": True, **fields}


def fail(
    message: str,
    *,
    kind: str = "error",
    line: int | None = None,
    column: int | None = None,
    **fields: Any,
) -> dict[str, Any]:
    """Failed tool payload (protocol success, Pine/runtime failure)."""
    body: dict[str, Any] = {"ok": False, "error": message, "kind": kind}
    if line is not None:
        body["line"] = line
    if column is not None:
        body["column"] = column
    body.update(fields)
    return body


def jsonable(value: Any, *, depth: int = 0) -> Any:
    """Coerce *value* to JSON-safe types (NaN/Inf → ``None``)."""
    if depth > 8:
        return str(value)[:120]
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            return None
        return value
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    item = getattr(value, "item", None)
    if callable(item) and getattr(value, "ndim", 1) == 0:
        try:
            return jsonable(item(), depth=depth + 1)
        except Exception:
            return str(value)[:80]
    if isinstance(value, dict):
        return {str(k): jsonable(v, depth=depth + 1) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [jsonable(v, depth=depth + 1) for v in value]
    tolist = getattr(value, "tolist", None)
    if callable(tolist):
        try:
            return jsonable(tolist(), depth=depth + 1)
        except Exception:
            pass
    return str(value)[:120]


def clamp_sample(n: int) -> int:
    """Bound plot sample length to ``[1, SAMPLE_MAX]``."""
    if n < 1:
        return 1
    if n > SAMPLE_MAX:
        return SAMPLE_MAX
    return n


def clamp_bars(n: int) -> int:
    """Bound synthetic/execute bar count."""
    if n < 2:
        return 2
    if n > BARS_MAX:
        return BARS_MAX
    return n


def clamp_timeout(seconds: float | None) -> float:
    """Bound execute timeout; default 30s, cap 120s."""
    if seconds is None:
        return TIMEOUT_DEFAULT
    if seconds <= 0:
        return TIMEOUT_DEFAULT
    if seconds > TIMEOUT_MAX:
        return TIMEOUT_MAX
    return float(seconds)


def summarize_series(series: Any, *, sample: int = SAMPLE_DEFAULT) -> dict[str, Any]:
    """Last-N sample plus length / last / na count for one plot series."""
    n_keep = clamp_sample(sample)
    try:
        iterable = series is not None and hasattr(series, "__iter__") and not isinstance(series, (str, bytes, dict))
        values = list(series) if iterable else [series]
    except Exception:
        return {"len": 0, "last": None, "na_count": 0, "sample": []}
    coerced = [jsonable(v) for v in values]
    na_count = sum(1 for v in coerced if v is None)
    return {
        "len": len(coerced),
        "last": coerced[-1] if coerced else None,
        "na_count": na_count,
        "sample": coerced[-n_keep:],
    }


_LIST_EXTRA_KEYS = frozenset(
    {
        "events",
        "drawings",
        "alerts",
        "alert_conditions",
        "inputs",
        "logs",
        "__trades",
        "__events",
        "__drawings",
        "__alerts",
    }
)
_TRADE_LIST_KEYS = frozenset(
    {
        "events",
        "drawings",
        "alerts",
        "__trades",
        "__events",
        "__drawings",
        "__alerts",
    }
)
_COMPILED_META_KEYS = frozenset(
    {
        "auto_backend",
        "compile_fallback_reason",
        "timed_out",
        "error",
        "error_kind",
        "error_type",
        "error_bar",
        "error_bar_time",
    }
)


def _is_runtime_envelope(result: dict[str, Any]) -> bool:
    """True when *result* is a ``Runtime.run`` envelope (plots + named series)."""
    series = result.get("series")
    if not isinstance(series, dict):
        return False
    mode = result.get("mode")
    return "plots" in result or mode in ("interpret", "compile", "auto")


def _put_list_extra(extras: dict[str, Any], name: str, value: Any, *, include_trades: bool) -> None:
    try:
        seq = list(value) if value is not None else []
    except Exception:
        extras[name] = jsonable(value)
        return
    extras[name + "_count"] = len(seq)
    if include_trades and name in _TRADE_LIST_KEYS:
        extras[name] = jsonable(seq[:200])


def _put_extra(extras: dict[str, Any], name: str, value: Any, *, include_trades: bool) -> None:
    if name == "generated_code" and isinstance(value, str):
        extras["generated_code_chars"] = len(value)
        return
    if name in _LIST_EXTRA_KEYS:
        _put_list_extra(extras, name, value, include_trades=include_trades)
        return
    extras[name] = jsonable(value)


def split_plots(
    result: dict[str, Any] | None,
    *,
    sample: int = SAMPLE_DEFAULT,
    include_trades: bool = False,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Split a Runtime/compile result into truncated plots and extras.

    ``Runtime.run`` returns an envelope with ``plots`` (first series list) and
    ``series`` (named plot map) plus metadata (``compile_ms``, ``events``, …).
    ``CompiledScript.run`` returns a plot-name map plus ``__*`` strategy keys.
    """
    plots: dict[str, Any] = {}
    extras: dict[str, Any] = {}
    if not result:
        return plots, extras
    if _is_runtime_envelope(result):
        series = result.get("series") or {}
        for key, value in series.items():
            name = str(key)
            if name.startswith("__"):
                _put_extra(extras, name, value, include_trades=include_trades)
                continue
            plots[name] = summarize_series(value, sample=sample)
        if not plots:
            raw = result.get("plots")
            if raw is not None and not isinstance(raw, dict):
                plots["plot"] = summarize_series(raw, sample=sample)
        for key, value in result.items():
            if key in ("plots", "series"):
                continue
            _put_extra(extras, str(key), value, include_trades=include_trades)
        return plots, extras
    for key, value in result.items():
        name = str(key)
        if name in _COMPILED_META_KEYS or name.startswith("__"):
            _put_extra(extras, name, value, include_trades=include_trades)
        else:
            plots[name] = summarize_series(value, sample=sample)
    return plots, extras


def strip_secrets(payload: dict[str, Any]) -> dict[str, Any]:
    """Redact credential-like keys from an echoed argument dict."""
    out: dict[str, Any] = {}
    for key, value in payload.items():
        if str(key).lower().replace("-", "_") in _SECRET_KEYS:
            out[key] = "***"
        elif isinstance(value, dict):
            out[key] = strip_secrets(value)
        else:
            out[key] = value
    return out
