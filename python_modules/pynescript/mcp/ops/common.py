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

"""Shared source loading, synthetic OHLCV, and script-declaration helpers."""

from __future__ import annotations

import re

from pathlib import Path
from typing import Any

from pynescript.ast import node as ast
from pynescript.ast.error import SyntaxError as PineSyntaxError
from pynescript.mcp.serialize import fail


_VERSION_RE = re.compile(r"(?m)^//@version\s*=\s*(\d+)")
_DECL_NAMES = frozenset({"indicator", "strategy", "library", "study"})


def load_source(
    source: str | None = None,
    path: str | None = None,
    *,
    encoding: str = "utf-8",
) -> tuple[str, str] | dict[str, Any]:
    """Return ``(text, label)`` or a fail envelope if neither input is usable."""
    if source is not None and source != "":
        return source, path or "<mcp>"
    if path:
        p = Path(path)
        if not p.is_file():
            return fail(f"not found: {path}", kind="io")
        try:
            return p.read_text(encoding=encoding), str(p)
        except OSError as exc:
            return fail(str(exc), kind="io")
    return fail("source or path is required", kind="io")


def parse_error_fields(exc: BaseException) -> dict[str, Any]:
    """Extract line/column from a Pine :class:`SyntaxError` when present."""
    extra: dict[str, Any] = {}
    if isinstance(exc, PineSyntaxError):
        details = getattr(exc, "details", None)
        if details is not None:
            extra["line"] = details.lineno
            extra["column"] = details.offset
        extra["error"] = exc.message if getattr(exc, "message", None) else str(exc).split("\n")[0]
    else:
        extra["error"] = str(exc).split("\n")[0][:400]
    return extra


def synthetic_ohlcv_cols(n: int) -> dict[str, list[float]]:
    """Deterministic walk matching CLI ``pyne run`` bars."""
    ohlcv: dict[str, list[float]] = {"open": [], "high": [], "low": [], "close": [], "volume": []}
    price = 100.0
    for i in range(n):
        o = round(price, 2)
        c = round(price + (1.0 if i % 3 else -0.5), 2)
        h = round(max(o, c) + 0.8, 2)
        lo = round(min(o, c) - 0.8, 2)
        ohlcv["open"].append(float(o))
        ohlcv["high"].append(float(h))
        ohlcv["low"].append(float(max(lo, 0.01)))
        ohlcv["close"].append(float(c))
        ohlcv["volume"].append(float(1000 + i))
        price = c
    return ohlcv


def synthetic_ohlcv_rows(n: int) -> list[dict[str, Any]]:
    """Row-wise synthetic OHLCV for :class:`~pynescript.runtime.Runtime`."""
    cols = synthetic_ohlcv_cols(n)
    rows: list[dict[str, Any]] = []
    for i in range(n):
        rows.append(
            {
                "time": 1_700_000_000 + i * 60,
                "open": cols["open"][i],
                "high": cols["high"][i],
                "low": cols["low"][i],
                "close": cols["close"][i],
                "volume": cols["volume"][i],
            }
        )
    return rows


def version_from_source(source: str) -> int | None:
    """Parse ``//@version=N`` from the first matching line."""
    match = _VERSION_RE.search(source)
    if match is None:
        return None
    return int(match.group(1))


def _call_name(func: Any) -> str | None:
    if isinstance(func, ast.Name):
        return str(func.id)
    if isinstance(func, ast.Attribute):
        parent = _call_name(func.value)
        attr = getattr(func, "attr", None)
        if parent and attr:
            return f"{parent}.{attr}"
        if attr:
            return str(attr)
    return None


def _const_str(node: Any) -> str | None:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    return None


def _call_title(call: ast.Call) -> str | None:
    for item in call.args or []:
        if not isinstance(item, ast.Arg):
            continue
        if item.name in (None, "title", "shorttitle"):
            text = _const_str(item.value)
            if text is not None:
                return text
    return None


def script_declaration(tree: Any) -> tuple[str | None, str | None]:
    """Return ``(kind, title)`` from the first indicator/strategy/library/study call."""
    body = getattr(tree, "body", None) or []
    for stmt in body:
        value = getattr(stmt, "value", None) if isinstance(stmt, ast.Expr) else None
        if not isinstance(value, ast.Call):
            continue
        name = _call_name(value.func)
        if name in _DECL_NAMES:
            kind = "indicator" if name == "study" else name
            return kind, _call_title(value)
    return None, None


def call_path(call: ast.Call) -> str | None:
    """Dotted path of a Call func (``plot``, ``input.int``, ``strategy.entry``)."""
    return _call_name(call.func)
