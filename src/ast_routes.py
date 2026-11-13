# Copyright (c) 2026 HOOX · PYNE · jango-blockchained
# SPDX-License-Identifier: AGPL-3.0-or-later

"""POST /ast/* language ops for pyne-agent-worker (parse / lint / dump / …).

Wraps ``pynescript.mcp.ops.language`` + ``compile`` so the edge Worker can
serve the Grandmaster AST client without inventing a second contract.
``emit: true`` is ignored — never return Python IR from this surface.
"""

from __future__ import annotations

from typing import Any


_AST_OPS = frozenset(
    {
        "parse",
        "dump",
        "unparse",
        "format",
        "lint",
        "diagnostics",
        "compile",
    }
)


def is_ast_op(op: str | None) -> bool:
    return (op or "").strip().lower() in _AST_OPS


def _script_from_body(data: dict[str, Any]) -> str | None:
    raw = data.get("script")
    if raw is None:
        raw = data.get("source")
    if raw is None:
        raw = data.get("code")
    if not isinstance(raw, str):
        return None
    return raw


def _fail_body(message: str, *, kind: str = "io", **extra: Any) -> dict[str, Any]:
    body: dict[str, Any] = {"ok": False, "error": message, "kind": kind}
    body.update(extra)
    return body


def dispatch_ast(op: str, data: dict[str, Any]) -> dict[str, Any]:
    """Run one language op. Always returns a JSON-safe dict (``ok`` field)."""
    name = (op or "").strip().lower()
    if name not in _AST_OPS:
        return {
            "ok": False,
            "error": f"unknown ast op: {op}",
            "code": "UNKNOWN_ROUTE",
            "kind": "io",
        }

    script = _script_from_body(data)
    if script is None:
        return _fail_body("script (or source) string required", kind="io")
    if not script.strip():
        return _fail_body("empty script", kind="io")

    try:
        if name == "parse":
            from pynescript.mcp.ops.language import parse_source

            return parse_source(source=script)

        if name == "dump":
            from pynescript.mcp.ops.language import dump_source

            as_json = data.get("as_json")
            if as_json is None:
                as_json = True
            return dump_source(source=script, as_json=bool(as_json))

        if name in ("unparse", "format"):
            from pynescript.mcp.ops.language import format_source

            return format_source(source=script)

        if name == "lint":
            from pynescript.mcp.ops.language import lint_source

            result = lint_source(source=script)
            if int(result.get("error_count") or 0) > 0:
                result = dict(result)
                result["ok"] = False
                findings = result.get("findings") or []
                err = next(
                    (
                        f
                        for f in findings
                        if isinstance(f, dict)
                        and str(f.get("severity") or "").lower() == "error"
                    ),
                    None,
                )
                if err:
                    result["error"] = err.get("message") or "lint error"
                    result["kind"] = "lint"
                    if err.get("line") is not None:
                        result["line"] = err.get("line")
                    if err.get("column") is not None:
                        result["column"] = err.get("column")
            return result

        if name == "diagnostics":
            from pynescript.mcp.ops.language import diagnostics_source

            result = diagnostics_source(source=script)
            diags = result.get("diagnostics") or []
            errors = [
                d
                for d in diags
                if isinstance(d, dict)
                and str(d.get("severity") or "").lower() == "error"
            ]
            result = dict(result)
            result["error_count"] = len(errors)
            result["warning_count"] = max(0, len(diags) - len(errors))
            if errors:
                result["ok"] = False
                result["error"] = errors[0].get("message") or "diagnostics error"
                result["kind"] = "parse" if errors[0].get("code") == "parse" else "lint"
                if errors[0].get("line") is not None:
                    result["line"] = errors[0].get("line")
                if errors[0].get("column") is not None:
                    result["column"] = errors[0].get("column")
            return result

        if name == "compile":
            from pynescript.mcp.ops.compile import compile_source

            # Never emit Python IR on the Worker surface.
            return compile_source(source=script, emit=False)

    except Exception as exc:  # noqa: BLE001 — edge boundary
        return _fail_body(
            f"{type(exc).__name__}: {exc}",
            kind="runtime",
        )

    return _fail_body(f"unhandled ast op: {name}", kind="io")
