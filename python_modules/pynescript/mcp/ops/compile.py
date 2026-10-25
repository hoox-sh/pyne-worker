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

"""Compile, emit, prewarm, and CLI-style synthetic ``run``."""

from __future__ import annotations

import time

from typing import Any

from pynescript.mcp.ops.common import load_source
from pynescript.mcp.ops.common import parse_error_fields
from pynescript.mcp.ops.common import synthetic_ohlcv_cols
from pynescript.mcp.serialize import BARS_MAX
from pynescript.mcp.serialize import clamp_bars
from pynescript.mcp.serialize import fail
from pynescript.mcp.serialize import jsonable
from pynescript.mcp.serialize import ok
from pynescript.mcp.serialize import split_plots


def compile_source(
    source: str | None = None,
    path: str | None = None,
    *,
    encoding: str = "utf-8",
    emit: bool = False,
) -> dict[str, Any]:
    """Compile-check or transpile Pine (CLI ``compile`` / ``--emit``)."""
    from pynescript.compiler.engine import compile_script
    from pynescript.compiler.engine import has_numba
    from pynescript.compiler.engine import transpile

    loaded = load_source(source, path, encoding=encoding)
    if isinstance(loaded, dict):
        return loaded
    text, label = loaded
    t0 = time.perf_counter()
    try:
        if emit:
            code = transpile(text)
            elapsed = (time.perf_counter() - t0) * 1000
            return ok(
                file=label,
                emit=True,
                python=code,
                chars=len(code),
                elapsed_ms=round(elapsed, 2),
                numba=has_numba(),
            )
        compiled = compile_script(text)
        elapsed = (time.perf_counter() - t0) * 1000
        return ok(
            file=label,
            emit=False,
            mode="object" if compiled.object_mode else "numeric",
            plots=list(compiled.plot_titles or []),
            elapsed_ms=round(elapsed, 2),
            numba=has_numba(),
            nopython_fallback_reason=compiled.nopython_fallback_reason,
        )
    except Exception as exc:
        fields = parse_error_fields(exc)
        return fail(
            f"{type(exc).__name__}: {fields.get('error') or exc}",
            kind="compile",
            line=fields.get("line"),
            column=fields.get("column"),
            file=label,
        )


def prewarm_sources(paths: list[str] | None = None, *, encoding: str = "utf-8", force: bool = False) -> dict[str, Any]:
    """Warm Numba builtins and optional script IR caches (CLI ``prewarm``)."""
    from pynescript.compiler.engine import compile_cache_stats
    from pynescript.compiler.engine import prewarm_scripts

    sources: list[str] = []
    errors: list[str] = []
    for raw in paths or []:
        loaded = load_source(None, raw, encoding=encoding)
        if isinstance(loaded, dict):
            errors.append(str(loaded.get("error") or raw))
            continue
        sources.append(loaded[0])
    t0 = time.perf_counter()
    result = prewarm_scripts(sources or None, force_builtins=force)
    elapsed = (time.perf_counter() - t0) * 1000
    payload = {**result, "prewarm_ms": round(elapsed, 2), "cache": compile_cache_stats()}
    if errors:
        payload["path_errors"] = errors
    if result.get("scripts_failed"):
        return fail("one or more scripts failed to prewarm", kind="compile", **payload)
    return ok(**payload)


def run_synthetic(
    source: str | None = None,
    path: str | None = None,
    *,
    encoding: str = "utf-8",
    bars: int = 50,
    sample: int = 8,
) -> dict[str, Any]:
    """Compile + execute on synthetic OHLCV (CLI ``pyne run``)."""
    from pynescript.compiler.engine import compile_script

    loaded = load_source(source, path, encoding=encoding)
    if isinstance(loaded, dict):
        return loaded
    text, label = loaded
    n = clamp_bars(bars)
    if bars > BARS_MAX:
        return fail(f"bars cap is {BARS_MAX}", kind="io")
    ohlcv = synthetic_ohlcv_cols(n)
    t0 = time.perf_counter()
    try:
        compiled = compile_script(text)
        result = compiled.run(
            ohlcv["open"],
            ohlcv["high"],
            ohlcv["low"],
            ohlcv["close"],
            ohlcv["volume"],
        )
    except Exception as exc:
        fields = parse_error_fields(exc)
        return fail(
            f"{type(exc).__name__}: {fields.get('error') or exc}",
            kind="runtime",
            line=fields.get("line"),
            column=fields.get("column"),
            file=label,
        )
    elapsed = (time.perf_counter() - t0) * 1000
    plots, extras = split_plots(result if isinstance(result, dict) else {}, sample=sample)
    return ok(
        file=label,
        bars=n,
        mode="object" if compiled.object_mode else "numeric",
        elapsed_ms=round(elapsed, 2),
        plots=plots,
        extras=jsonable(extras),
    )
