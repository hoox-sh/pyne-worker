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

"""Parse, format, convert, lint, dump, symbols, diagnostics."""

from __future__ import annotations

import json

from pathlib import Path
from typing import Any

from pynescript.ast import dump
from pynescript.ast import node as ast
from pynescript.ast import parse
from pynescript.ast import unparse
from pynescript.ast import walk
from pynescript.ast.error import SyntaxError as PineSyntaxError
from pynescript.ast.linter import lint_script
from pynescript.mcp.ops.common import call_path
from pynescript.mcp.ops.common import load_source
from pynescript.mcp.ops.common import parse_error_fields
from pynescript.mcp.ops.common import script_declaration
from pynescript.mcp.ops.common import version_from_source
from pynescript.mcp.serialize import fail
from pynescript.mcp.serialize import jsonable
from pynescript.mcp.serialize import ok
from pynescript.util.pine_convert import convert_pine


_TREE_JSON_MAX = 100_000


def _fail_parse(exc: BaseException, label: str) -> dict[str, Any]:
    fields = parse_error_fields(exc)
    return fail(
        str(fields.get("error") or exc),
        kind="parse",
        line=fields.get("line"),
        column=fields.get("column"),
        file=label,
    )


_SYMBOL_CALLS = frozenset(
    {
        "plot",
        "hline",
        "plotshape",
        "plotchar",
        "plotcandle",
        "plotbar",
        "bgcolor",
        "fill",
        "input",
        "input.int",
        "input.float",
        "input.bool",
        "input.string",
        "input.color",
        "input.source",
        "input.timeframe",
        "input.session",
        "input.symbol",
        "strategy.entry",
        "strategy.exit",
        "strategy.close",
        "strategy.order",
        "alertcondition",
        "alert",
    }
)


def parse_source(source: str | None = None, path: str | None = None, *, encoding: str = "utf-8") -> dict[str, Any]:
    """Structured parse with kind/title/version and located errors."""
    loaded = load_source(source, path, encoding=encoding)
    if isinstance(loaded, dict):
        return loaded
    text, label = loaded
    try:
        tree = parse(text, label)
    except (PineSyntaxError, SyntaxError, ValueError) as exc:
        fields = parse_error_fields(exc)
        return fail(
            str(fields.get("error") or exc),
            kind="parse",
            line=fields.get("line"),
            column=fields.get("column"),
            file=label,
            errors=[
                {
                    "message": fields.get("error") or str(exc).split("\n")[0],
                    "line": fields.get("line"),
                    "column": fields.get("column"),
                }
            ],
        )
    kind, title = script_declaration(tree)
    return ok(
        file=label,
        version=version_from_source(text),
        kind=kind,
        title=title,
        errors=[],
    )


def check_sources(
    source: str | None = None,
    path: str | None = None,
    paths: list[str] | None = None,
    *,
    encoding: str = "utf-8",
    ext: str = ".pine",
) -> dict[str, Any]:
    """Parse-only validation for one source or many paths (CLI ``check``)."""
    files: list[str] = []
    if source:
        try:
            parse(source, path or "<mcp>")
            return ok(files=[{"path": path or "<mcp>", "ok": True}], ok_count=1, fail_count=0)
        except (PineSyntaxError, SyntaxError, ValueError) as exc:
            fields = parse_error_fields(exc)
            rec = {
                "path": path or "<mcp>",
                "ok": False,
                **{k: v for k, v in fields.items() if k != "error"},
                "error": fields.get("error"),
            }
            return ok(files=[rec], ok_count=0, fail_count=1)
    targets = list(paths or [])
    if path:
        targets.append(path)
    if not targets:
        return fail("source, path, or paths is required", kind="io")
    for raw in targets:
        p = Path(raw)
        if p.is_dir():
            files.extend(str(f) for f in sorted(p.rglob(f"*{ext}")) if f.is_file())
        else:
            files.append(raw)
    results: list[dict[str, Any]] = []
    ok_n = 0
    fail_n = 0
    for item in files:
        loaded = load_source(None, item, encoding=encoding)
        if isinstance(loaded, dict):
            results.append({"path": item, "ok": False, "error": loaded.get("error")})
            fail_n += 1
            continue
        text, label = loaded
        try:
            parse(text, label)
            results.append({"path": label, "ok": True})
            ok_n += 1
        except (PineSyntaxError, SyntaxError, ValueError, OSError) as exc:
            fields = parse_error_fields(exc)
            results.append(
                {
                    "path": label,
                    "ok": False,
                    "error": fields.get("error"),
                    "line": fields.get("line"),
                    "column": fields.get("column"),
                }
            )
            fail_n += 1
    return ok(files=results, ok_count=ok_n, fail_count=fail_n)


def format_source(source: str | None = None, path: str | None = None, *, encoding: str = "utf-8") -> dict[str, Any]:
    """Parse → unparse. Does not write disk."""
    loaded = load_source(source, path, encoding=encoding)
    if isinstance(loaded, dict):
        return loaded
    text, label = loaded
    try:
        formatted = unparse(parse(text, label))
    except (PineSyntaxError, SyntaxError, ValueError) as exc:
        return _fail_parse(exc, label)
    if not formatted.endswith("\n"):
        formatted += "\n"
    original = text if text.endswith("\n") else text + "\n"
    return ok(file=label, source=formatted, changed=original != formatted)


def convert_source(
    source: str | None = None,
    path: str | None = None,
    *,
    to: int | str = 6,
    encoding: str = "utf-8",
) -> dict[str, Any]:
    """Rewrite older Pine toward v5 or v6."""
    loaded = load_source(source, path, encoding=encoding)
    if isinstance(loaded, dict):
        return loaded
    text, label = loaded
    target = int(str(to).lstrip("v"))
    if target not in (5, 6):
        return fail("to must be 5 or 6", kind="io")
    converted = convert_pine(text, to=target)
    if not converted.endswith("\n"):
        converted += "\n"
    original = text if text.endswith("\n") else text + "\n"
    return ok(file=label, to=target, source=converted, changed=converted != original)


def lint_source(source: str | None = None, path: str | None = None, *, encoding: str = "utf-8") -> dict[str, Any]:
    """Lint findings as JSON (CLI ``lint --json``)."""
    loaded = load_source(source, path, encoding=encoding)
    if isinstance(loaded, dict):
        return loaded
    text, label = loaded
    warnings = lint_script(text, label)
    findings = [
        {
            "code": w.code,
            "message": w.message,
            "line": w.line,
            "column": w.column,
            "severity": w.severity,
            "file": label,
        }
        for w in warnings
    ]
    return ok(
        file=label,
        findings=findings,
        error_count=sum(1 for w in warnings if w.severity == "error"),
        warning_count=sum(1 for w in warnings if w.severity != "error"),
    )


def _node_to_json(node: Any, *, depth: int = 0) -> Any:
    if depth > 24:
        return {"_type": type(node).__name__, "_truncated": True}
    if node is None or isinstance(node, (str, int, float, bool)):
        return jsonable(node)
    if isinstance(node, (bytes, bytearray)):
        return bytes(node).decode("utf-8", errors="replace")
    if isinstance(node, list):
        return [_node_to_json(item, depth=depth + 1) for item in node]
    if not isinstance(node, ast.AST):
        return jsonable(node)
    payload: dict[str, Any] = {"_type": type(node).__name__}
    for field in getattr(node, "_fields", []) or []:
        payload[field] = _node_to_json(getattr(node, field, None), depth=depth + 1)
    lineno = getattr(node, "lineno", None)
    if lineno is not None:
        payload["lineno"] = lineno
        payload["col_offset"] = getattr(node, "col_offset", None)
    return payload


def dump_source(
    source: str | None = None,
    path: str | None = None,
    *,
    encoding: str = "utf-8",
    indent: int = 2,
    as_json: bool = True,
) -> dict[str, Any]:
    """AST dump text plus optional JSON tree (omitted when huge)."""
    loaded = load_source(source, path, encoding=encoding)
    if isinstance(loaded, dict):
        return loaded
    text, label = loaded
    try:
        tree = parse(text, label)
    except (PineSyntaxError, SyntaxError, ValueError) as exc:
        return _fail_parse(exc, label)
    dumped = dump(tree, indent=indent)
    payload = ok(file=label, dump=dumped)
    if as_json:
        tree_json = _node_to_json(tree)
        encoded = json.dumps(tree_json, default=str)
        if len(encoded) <= _TREE_JSON_MAX:
            payload["tree"] = tree_json
        else:
            payload["tree_truncated"] = True
            payload["tree_chars"] = len(encoded)
    return payload


def unparse_source(source: str | None = None, path: str | None = None, *, encoding: str = "utf-8") -> dict[str, Any]:
    """Round-trip parse → unparse (CLI ``parse-and-unparse``)."""
    return format_source(source, path, encoding=encoding)


def _arg_title(call: ast.Call) -> str | None:
    for item in call.args or []:
        if not isinstance(item, ast.Arg):
            continue
        if item.name in (None, "title", "id"):
            value = item.value
            if isinstance(value, ast.Constant) and isinstance(value.value, str):
                return value.value
    return None


def symbols_source(source: str | None = None, path: str | None = None, *, encoding: str = "utf-8") -> dict[str, Any]:
    """Outline: functions, types, enums, inputs, plots, strategy entries."""
    loaded = load_source(source, path, encoding=encoding)
    if isinstance(loaded, dict):
        return loaded
    text, label = loaded
    try:
        tree = parse(text, label)
    except (PineSyntaxError, SyntaxError, ValueError) as exc:
        return _fail_parse(exc, label)
    symbols: list[dict[str, Any]] = []
    for node in walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name:
            symbols.append({"kind": "function", "name": node.name, "line": node.lineno})
        elif isinstance(node, ast.TypeDef) and node.name:
            symbols.append({"kind": "type", "name": node.name, "line": node.lineno})
        elif isinstance(node, ast.EnumDef) and node.name:
            symbols.append({"kind": "enum", "name": node.name, "line": node.lineno})
        elif isinstance(node, ast.Call):
            name = call_path(node)
            if name in _SYMBOL_CALLS:
                kind = "input" if name.startswith("input") else "entry" if "strategy." in name else "plot"
                symbols.append(
                    {
                        "kind": kind,
                        "name": _arg_title(node) or name,
                        "call": name,
                        "line": getattr(node, "lineno", None),
                    }
                )
    return ok(file=label, symbols=symbols)


def diagnostics_source(
    source: str | None = None,
    path: str | None = None,
    *,
    encoding: str = "utf-8",
) -> dict[str, Any]:
    """Parse errors plus lint findings in one list."""
    loaded = load_source(source, path, encoding=encoding)
    if isinstance(loaded, dict):
        return loaded
    text, label = loaded
    items: list[dict[str, Any]] = []
    tree = None
    try:
        tree = parse(text, label)
    except (PineSyntaxError, SyntaxError, ValueError) as exc:
        fields = parse_error_fields(exc)
        items.append(
            {
                "code": "parse",
                "severity": "error",
                "message": fields.get("error") or str(exc).split("\n")[0],
                "line": fields.get("line"),
                "column": fields.get("column"),
                "file": label,
            }
        )
    warnings = lint_script(text, label, tree=tree)
    for w in warnings:
        items.append(
            {
                "code": w.code,
                "severity": w.severity,
                "message": w.message,
                "line": w.line,
                "column": w.column,
                "file": label,
            }
        )
    return ok(file=label, diagnostics=items)
