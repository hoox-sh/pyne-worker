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

"""Lightweight static checks for Pine Script source.

Public API:

* :class:`LintWarning` — one finding (code, message, location, severity)
* :class:`PineLinter` — stateful linter; call :meth:`PineLinter.lint`
* :func:`lint_script` / :func:`lint_file` — one-shot helpers

Rules are heuristic (regex + :func:`~pynescript.ast.helper.parse` for syntax).
This is not a full type checker; see :mod:`pynescript.ast.type_system` for
type modeling used by the evaluator.

Rule code prefixes: ``E`` errors, ``W`` warnings (version/deprecated), ``C`` style.
"""

from __future__ import annotations

import re

from dataclasses import dataclass
from typing import Any

from pynescript.ast import parse
from pynescript.ast.helper import walk
from pynescript.ast.node import Arg
from pynescript.ast.node import Attribute
from pynescript.ast.node import Call
from pynescript.ast.node import Conditional
from pynescript.ast.node import Constant
from pynescript.ast.node import If
from pynescript.ast.node import Name
from pynescript.ast.node import While


@dataclass
class LintWarning:
    """A single lint finding.

    Attributes:
        code: Stable rule id (e.g. ``E001``, ``W001``, ``C002``).
        message: Human-readable description.
        line: 1-based line if known.
        column: 0-based column if known.
        severity: ``"error"`` or ``"warning"`` (default).
    """

    code: str
    message: str
    line: int | None = None
    column: int | None = None
    severity: str = "warning"

    def __str__(self) -> str:
        location = f"line {self.line}" if self.line else "unknown location"
        return f"{self.severity}: [{self.code}] {self.message} at {location}"


class PineLinter:
    """Run built-in static analysis rules on Pine Script source.

    Each :meth:`lint` call resets :attr:`warnings` and returns the new list.
    Source is expected as a decoded Unicode string (UTF-8 when loaded from disk).
    """

    def __init__(self) -> None:
        """Create a linter with an empty :attr:`warnings` list."""
        self.warnings: list[LintWarning] = []

    def lint(self, source: str, filename: str = "<input>", *, tree: Any | None = None) -> list[LintWarning]:
        """Run all rules on *source* and return findings.

        Args:
            source: Full script text.
            filename: Label passed to the parser for syntax diagnostics.
            tree: Pre-parsed AST. When given, the syntax rule reuses it
                instead of re-parsing *source* (halves parse cost for
                callers that already have the tree, e.g. the language
                server workspace).

        Returns:
            List of :class:`LintWarning` (also stored on :attr:`warnings`).
        """
        self.warnings = []

        if tree is None:
            tree = self._check_syntax(source, filename)
        self._check_version(source)
        self._check_deprecated(source)
        self._check_naming(source)
        self._check_style(source)
        if tree is not None:
            self._check_v6_kwargs(tree)
            self._check_bool_conditions(tree)

        return self.warnings

    def _add_warning(
        self,
        code: str,
        message: str,
        line: int | None = None,
        column: int | None = None,
        severity: str = "warning",
    ) -> None:
        """Append a :class:`LintWarning` to :attr:`warnings`."""
        self.warnings.append(LintWarning(code=code, message=message, line=line, column=column, severity=severity))

    def _check_syntax(self, source: str, filename: str) -> Any | None:
        """Parse *source*; emit ``E001`` if :func:`~pynescript.ast.helper.parse` fails.

        Returns the parsed tree on success (reused by AST-based rules),
        else ``None``.
        """
        try:
            return parse(source, filename)
        except Exception as e:
            line: int | None = None
            column: int | None = None
            # Prefer structured location from pynescript.ast.error.SyntaxError
            details = getattr(e, "details", None)
            if details is not None:
                lineno = getattr(details, "lineno", None)
                offset = getattr(details, "offset", None)
                if isinstance(lineno, int) and lineno > 0:
                    line = lineno
                if isinstance(offset, int) and offset >= 0:
                    column = offset
            if line is None:
                # Fallback: "line N" / "line: N" in the message
                match = re.search(r"line[:\s]+(\d+)", str(e), re.IGNORECASE)
                if match:
                    line = int(match.group(1))
            # Prefer the short .message when available (avoid caret dump in chips)
            msg = getattr(e, "message", None)
            if not isinstance(msg, str) or not msg.strip():
                msg = str(e).split("\n", 1)[0].strip() or str(e)
            self._add_warning(
                code="E001",
                message=f"Syntax error: {msg}",
                line=line,
                column=column,
                severity="error",
            )
        return None

    def _check_version(self, source: str) -> None:
        """Require ``//@version=…``; warn if version is below 5 (``W001``/``W002``)."""
        version_match = re.search(r"//\s*@version\s*=\s*(\d+)", source)
        if not version_match:
            self._add_warning(
                code="W001",
                message="Missing @version declaration. Add '//@version=6' at the top.",
                line=1,
            )
        else:
            version = int(version_match.group(1))
            if version < 5:
                self._add_warning(
                    code="W002",
                    message=f"Pine Script v{version} is deprecated. Consider upgrading to v6.",
                    line=source[: version_match.start()].count("\n") + 1,
                )

    def _check_deprecated(self, source: str) -> None:
        """Flag known deprecated call patterns (``W101``).

        Retired: ``W102`` (histogram → plotcandle was wrong guidance —
        plotcandle is not a histogram substitute) and ``W103``
        (``var int x = na`` → 0 changes semantics; na-init is idiomatic).
        Codes stay retired so history stays greppable.
        """
        deprecated_patterns = [
            (r"\bsecurity\s*\(\s*'[A-Z]+:[A-Z]+'", "W101", "Use request.security() with explicit parameters"),
        ]

        for pattern, code, message in deprecated_patterns:
            for match in re.finditer(pattern, source, re.IGNORECASE):
                line_num = source[: match.start()].count("\n") + 1
                self._add_warning(code, message, line=line_num)

    def _check_v6_kwargs(self, tree: Any) -> None:
        """Flag v6-removed kwargs that the evaluator silently ignores (``W104``).

        * ``when=`` on ``strategy.*`` order calls (removed in v6; gate with
          an ``if`` block instead).
        * ``transp=`` on drawing/plot calls (removed in v6; use
          ``color.new(color, transp)`` instead). ``color.new`` / ``color()``
          keep a valid ``transp`` and are never flagged.
        """
        for node in walk(tree):
            if not isinstance(node, Call):
                continue
            func = _call_func_name(node.func)
            if func is None:
                continue
            kwargs = {a.name for a in node.args if isinstance(a, Arg) and a.name}
            if not kwargs:
                continue
            line = getattr(node, "lineno", None)
            head, _, _ = func.partition(".")
            if head == "strategy" and "when" in kwargs:
                self._add_warning(
                    code="W104",
                    message="strategy.* 'when=' was removed in v6 and is ignored; gate with an 'if' block instead",
                    line=line,
                )
            if "transp" in kwargs and func not in ("color.new", "color"):
                if head in ("line", "box", "label", "table", "polyline", "linefill") or func in (
                    "plot",
                    "plotarrow",
                    "plotbar",
                    "plotcandle",
                    "plotchar",
                    "plotshape",
                    "fill",
                    "bgcolor",
                    "barcolor",
                    "hline",
                ):
                    self._add_warning(
                        code="W104",
                        message="'transp=' was removed in v6 and is ignored; use color.new(color, transp) instead",
                        line=line,
                    )

    def _check_bool_conditions(self, tree: Any) -> None:
        """Flag numeric literals in boolean positions (``W105``).

        The evaluator stays permissive (``if 1`` runs the body), so v6
        strictness — explicit comparisons or ``bool()`` casts — is nudged
        here rather than enforced at runtime.
        """
        for node in walk(tree):
            if not isinstance(node, (If, While, Conditional)):
                continue
            test = node.test
            if isinstance(test, Constant) and isinstance(test.value, (int, float)) and not isinstance(test.value, bool):
                self._add_warning(
                    code="W105",
                    message="Numeric literal in boolean condition; use an explicit comparison or bool() cast",
                    line=getattr(node, "lineno", None),
                )

    def _check_naming(self, source: str) -> None:
        """Heuristic naming checks for ``ta.*`` assignments (``C001``)."""
        for i, line in enumerate(source.split("\n"), 1):
            eq = line.find("=")
            if eq <= 0:
                continue
            if line[eq - 1] == ":":
                continue
            lhs = line[:eq].rstrip(" \t")
            j = len(lhs)
            while j > 0 and (lhs[j - 1].isalnum() or lhs[j - 1] == "_"):
                j -= 1
            var_name = lhs[j:]
            if not var_name:
                continue
            rhs = line[eq + 1 :].lstrip(" \t")
            if not rhs.startswith("ta."):
                continue
            # snake_case only — ``fastMA`` / ``sma`` are already camel/short.
            if "_" in var_name and "a" <= var_name[0] <= "z":
                self._add_warning(
                    code="C001",
                    message=f"Variable '{var_name}' should use camelCase (e.g., '{_to_camel(var_name)}')",
                    line=i,
                )

    def _check_style(self, source: str) -> None:
        """Line length and trailing-newline style rules (``C002``, ``C004``).

        Retired: ``C003`` (indented ``if`` without “braces” — Pine has no
        braces, so the rule fired on every normal block). Code stays
        retired so history stays greppable.
        """
        lines = source.split("\n")
        for i, line in enumerate(lines, 1):
            if len(line.rstrip()) > 120:
                self._add_warning(
                    code="C002",
                    message=f"Line exceeds 120 characters ({len(line.rstrip())})",
                    line=i,
                )

        # Do not strip() before the check — strip removes the trailing newline.
        if source and not source.endswith("\n"):
            self._add_warning(
                code="C004",
                message="File should end with a newline",
            )


def _call_func_name(func: Any) -> str | None:
    """Build ``a.b.c`` from Attribute/Name nodes without evaluating values."""
    parts: list[str] = []
    cur = func
    while isinstance(cur, Attribute):
        parts.append(cur.attr)
        cur = cur.value
    if not isinstance(cur, Name):
        return None
    parts.append(cur.id)
    parts.reverse()
    return ".".join(parts)


def _to_camel(name: str) -> str:
    """Convert snake_case *name* to camelCase for suggestion messages."""
    components = name.split("_")
    return components[0] + "".join(x.title() for x in components[1:])


def lint_script(source: str, filename: str = "<input>", *, tree: Any | None = None) -> list[LintWarning]:
    """Lint a source string; convenience wrapper around :class:`PineLinter`.

    Args:
        source: Decoded Pine Script text.
        filename: Label for diagnostics.
        tree: Pre-parsed AST; skips the internal re-parse when given.

    Returns:
        Findings from a fresh linter instance.
    """
    linter = PineLinter()
    return linter.lint(source, filename, tree=tree)


def lint_file(filepath: str) -> list[LintWarning]:
    """Lint a file read as UTF-8.

    Args:
        filepath: Path to a ``.pine`` (or other) source file.

    Returns:
        Findings; *filepath* is used as the diagnostic filename.
    """
    with open(filepath, encoding="utf-8") as f:
        source = f.read()

    return lint_script(source, filepath)
