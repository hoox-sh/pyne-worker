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

"""Folding ranges — ``textDocument/foldingRange``.

Public handler: :func:`handle_folding_range`. Emits line folds for block
constructs (``If`` / ``ForTo`` / ``ForIn`` / ``While`` / ``Switch`` / ``Once``,
plus ``FunctionDef`` / ``TypeDef`` / ``EnumDef``) and cheap ``//#region`` /
``# region`` comment pairs (kind ``region``).

Control blocks are expressions and often sit under :class:`~pynescript.ast.node.Expr`;
the walker visits the inner node. Empty source or parse failure returns ``[]``.
"""

from __future__ import annotations

import logging
import re

from typing import Any

from lsprotocol import types as lsp

from pynescript.ast import node as ast
from pynescript.ast.helper import parse
from pynescript.ast.helper import walk


logger = logging.getLogger(__name__)

_FOLDABLE: tuple[type[ast.AST], ...] = (
    ast.If,
    ast.ForTo,
    ast.ForIn,
    ast.While,
    ast.Switch,
    ast.Once,
    ast.FunctionDef,
    ast.TypeDef,
    ast.EnumDef,
)

# VS Code language-configuration markers plus HASH_COMMENT ``# region``.
_REGION_START = re.compile(r"^\s*(?://\s*#?|#\s*)region\b", re.IGNORECASE)
_REGION_END = re.compile(r"^\s*(?://\s*#?|#\s*)endregion\b", re.IGNORECASE)
_ELSE_LINE = re.compile(r"^\s*else\b")


def handle_folding_range(
    params: lsp.FoldingRangeParams,
    source: str | None,
    tree: Any | None = ...,
) -> list[lsp.FoldingRange]:
    """Handle textDocument/foldingRange request.

    Args:
        params: Client folding-range params.
        source: Document text, or ``None``.
        tree: Pre-parsed AST from the workspace cache. Pass ``None`` when the
            workspace already failed to parse (skips a redundant re-parse).
            Omit (default ``...``) to parse from *source*.

    Returns:
        Folding ranges (possibly empty). Never ``None``.
    """
    if tree is ...:
        if not source:
            return []
        try:
            tree = parse(source, filename=params.text_document.uri)
        except Exception as exc:
            logger.debug("folding_range: parse failed: %s", exc)
            return []
    if tree is None:
        return []

    ranges: list[lsp.FoldingRange] = []
    lines = source.splitlines() if source else None
    for node in walk(tree):
        if isinstance(node, _FOLDABLE):
            rng = _fold_for_node(node, lines)
            if rng is not None:
                ranges.append(rng)
    if source:
        ranges.extend(_region_folds(source))
    ranges.sort(key=lambda r: (r.start_line, r.end_line, r.kind or ""))
    return ranges


def _fold_for_node(node: ast.AST, lines: list[str] | None) -> lsp.FoldingRange | None:
    """Fold from the keyword line through the last line of the construct.

    Parser ``end_lineno`` often lands on the next statement (exclusive end, or
    a few characters into it) and swallows trailing comments. When source
    lines are available, clamp to the indented body (``else`` at the header
    indent continues an ``If``). Single-line constructs are skipped.
    """
    lineno = getattr(node, "lineno", None)
    if not lineno:
        return None
    start_line = lineno - 1
    if lines is not None:
        end_line = _indent_end(lines, start_line)
    else:
        end_line = _raw_end_line(node)
    if end_line <= start_line:
        return None
    return lsp.FoldingRange(start_line=start_line, end_line=end_line)


def _raw_end_line(node: ast.AST) -> int:
    """0-based last line from AST locations (fallback when source is absent)."""
    lineno = getattr(node, "lineno", None) or 1
    start_line = lineno - 1
    end_lineno = getattr(node, "end_lineno", None) or lineno
    end_col = getattr(node, "end_col_offset", None)
    end_line = end_lineno - 1
    if (end_col or 0) == 0 and end_line > start_line:
        end_line -= 1
    return max(start_line, end_line)


def _line_indent(line: str) -> int:
    return len(line) - len(line.lstrip(" \t"))


def _indent_end(lines: list[str], start_line: int) -> int:
    """Last 0-based line of the indented block starting at *start_line*."""
    if start_line < 0 or start_line >= len(lines):
        return start_line
    header_indent = _line_indent(lines[start_line])
    last = start_line
    for i in range(start_line + 1, len(lines)):
        line = lines[i]
        if not line.strip():
            continue
        ind = _line_indent(line)
        if ind > header_indent:
            last = i
            continue
        if ind == header_indent and _ELSE_LINE.match(line):
            last = i
            continue
        break
    return last


def _region_folds(source: str) -> list[lsp.FoldingRange]:
    """Fold ``//#region`` / ``# region`` pairs by scanning source lines."""
    ranges: list[lsp.FoldingRange] = []
    stack: list[int] = []
    for i, line in enumerate(source.splitlines()):
        if _REGION_END.match(line):
            if stack:
                start = stack.pop()
                if i > start:
                    ranges.append(
                        lsp.FoldingRange(
                            start_line=start,
                            end_line=i,
                            kind=lsp.FoldingRangeKind.Region,
                        )
                    )
        elif _REGION_START.match(line):
            stack.append(i)
    return ranges
