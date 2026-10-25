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

"""In-process MCP tool dispatch (no stdio, no MCP SDK)."""

from __future__ import annotations

import inspect
import json

from collections.abc import Callable
from typing import Any

from pynescript.mcp.catalog import TOOL_NAMES
from pynescript.mcp.serialize import fail
from pynescript.mcp.serialize import jsonable


_HANDLERS: dict[str, Callable[..., Any]] = {}


def resolve_tool_name(name: str) -> str | None:
    """Map ``lint`` / ``pyne_lint`` / ``pyne-lint`` onto a catalog tool name."""
    raw = (name or "").strip()
    if not raw:
        return None
    key = raw.replace("-", "_")
    if key in TOOL_NAMES:
        return key
    if not key.startswith("pyne_"):
        prefixed = f"pyne_{key}"
        if prefixed in TOOL_NAMES:
            return prefixed
    return None


def _handlers() -> dict[str, Callable[..., Any]]:
    if _HANDLERS:
        return _HANDLERS
    from pynescript.mcp import server as mcp_server

    missing = [n for n in TOOL_NAMES if not callable(getattr(mcp_server, n, None))]
    if missing:
        msg = f"MCP catalog tools missing on server: {missing}"
        raise RuntimeError(msg)
    _HANDLERS.update({n: getattr(mcp_server, n) for n in TOOL_NAMES})
    return _HANDLERS


def coerce_cli_value(raw: str) -> Any:
    """JSON-decode *raw* when possible, otherwise keep the string."""
    text = raw.strip()
    if text == "":
        return ""
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return raw


def parse_kv(item: str) -> tuple[str, Any]:
    """Split ``KEY=VALUE``; value is JSON-coerced."""
    key, sep, rest = item.partition("=")
    name = key.strip()
    if not name or sep != "=":
        msg = f"expected KEY=VALUE, got {item!r}"
        raise ValueError(msg)
    return name, coerce_cli_value(rest)


def _bind(fn: Callable[..., Any], arguments: dict[str, Any]) -> dict[str, Any]:
    spec = inspect.signature(fn)
    if any(p.kind == inspect.Parameter.VAR_KEYWORD for p in spec.parameters.values()):
        return dict(arguments)
    allowed = {
        name
        for name, param in spec.parameters.items()
        if param.kind
        in (
            inspect.Parameter.POSITIONAL_ONLY,
            inspect.Parameter.POSITIONAL_OR_KEYWORD,
            inspect.Parameter.KEYWORD_ONLY,
        )
    }
    return {k: v for k, v in arguments.items() if k in allowed}


def call_tool(name: str, arguments: dict[str, Any] | None = None) -> dict[str, Any]:
    """Invoke one catalog tool and return its JSON envelope.

    Does not require the ``mcp`` extra. Pine/runtime failures are ``ok: false``.
    """
    resolved = resolve_tool_name(name)
    if resolved is None:
        return fail(f"unknown tool: {name}", kind="io", tools=list(TOOL_NAMES))
    fn = _handlers()[resolved]
    bound = _bind(fn, dict(arguments or {}))
    try:
        result = fn(**bound)
    except TypeError as exc:
        return fail(str(exc), kind="io", tool=resolved)
    except Exception as exc:
        return fail(f"{type(exc).__name__}: {exc}", kind="error", tool=resolved)
    if isinstance(result, dict):
        return result
    return {"ok": True, "result": jsonable(result), "tool": resolved}
