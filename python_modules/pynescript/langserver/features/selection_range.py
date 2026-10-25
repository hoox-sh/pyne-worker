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

"""Selection ranges — ``textDocument/selectionRange``.

Public handler: :func:`handle_selection_range`. Expands from the identifier
(or smallest AST node) under the cursor to the enclosing statement, then
enclosing blocks (``If`` / ``ForTo`` / ``ForIn`` / ``While`` / ``Switch`` /
``Once`` / ``FunctionDef`` / ``TypeDef`` / ``EnumDef``).

Ranges use AST ``lineno`` / ``end_lineno`` / ``col_offset`` / ``end_col_offset``
(same mapping as :mod:`.symbols`). Empty source, parse failure, or no node at
the position returns ``[]``.
"""

from __future__ import annotations

import logging

from typing import Any

from lsprotocol import types as lsp

from pynescript.ast import node as ast
from pynescript.ast.helper import parse
from pynescript.ast.helper import walk
from pynescript.langserver.protocol.utils import get_identifier_segment_at_position

from ._ranges import node_to_range as _node_to_range


logger = logging.getLogger(__name__)

_BLOCKS: tuple[type[ast.AST], ...] = (
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


def handle_selection_range(
    params: lsp.SelectionRangeParams,
    source: str | None,
    tree: Any | None = ...,
) -> list[lsp.SelectionRange]:
    """Handle textDocument/selectionRange request.

    Args:
        params: Client selection-range params (one or more positions).
        source: Document text, or ``None``.
        tree: Pre-parsed AST from the workspace cache. Pass ``None`` when the
            workspace already failed to parse (skips a redundant re-parse).
            Omit (default ``...``) to parse from *source*.

    Returns:
        One :class:`~lsprotocol.types.SelectionRange` per position that has a
        containing node (possibly empty). Never ``None``.
    """
    if tree is ...:
        if not source:
            return []
        try:
            tree = parse(source, filename=params.text_document.uri)
        except Exception as exc:
            logger.debug("selection_range: parse failed: %s", exc)
            return []
    if tree is None:
        return []

    results: list[lsp.SelectionRange] = []
    for pos in params.positions:
        sr = _selection_for_position(tree, source, pos)
        if sr is not None:
            results.append(sr)
    return results


def _selection_for_position(
    tree: Any,
    source: str | None,
    pos: lsp.Position,
) -> lsp.SelectionRange | None:
    """Build an inner-to-outer selection chain for *pos*, or ``None``."""
    innermost = _ident_range(source, pos) if source else None
    located = _nodes_covering(tree, pos, innermost)
    if not located:
        return None
    if innermost is None:
        located.sort(key=lambda item: (_span_size(item[0]), _range_key(item[0])))
        innermost = located[0][0]
    nested = _nest_ranges(_chain_ranges(located, innermost))
    return _to_selection_range(nested)


def _nodes_covering(
    tree: Any,
    pos: lsp.Position,
    innermost: lsp.Range | None,
) -> list[tuple[lsp.Range, ast.AST]]:
    """AST nodes whose span contains *innermost* (or *pos* when no ident)."""
    located: list[tuple[lsp.Range, ast.AST]] = []
    for node in walk(tree):
        rng = _node_to_range(node)
        if rng is None:
            continue
        if innermost is not None:
            if _contains_range(rng, innermost):
                located.append((rng, node))
        elif _contains_pos(rng, pos):
            located.append((rng, node))
    return located


def _chain_ranges(
    located: list[tuple[lsp.Range, ast.AST]],
    innermost: lsp.Range,
) -> list[lsp.Range]:
    """Ident/smallest node, enclosing statement, then enclosing blocks."""
    ranges: list[lsp.Range] = [innermost]
    stmts = [rng for rng, node in located if isinstance(node, ast.stmt) and not _same_range(rng, innermost)]
    stmts.sort(key=lambda rng: (_span_size(rng), _range_key(rng)))
    if stmts:
        ranges.append(stmts[0])
    for rng, node in located:
        if isinstance(node, _BLOCKS) and not any(_same_range(rng, existing) for existing in ranges):
            ranges.append(rng)
    return ranges


def _ident_range(source: str, pos: lsp.Position) -> lsp.Range | None:
    """Character-precise identifier segment at *pos*, or ``None``."""
    segment, start, end, _full = get_identifier_segment_at_position(source, pos.line, pos.character)
    if not segment:
        return None
    return lsp.Range(
        start=lsp.Position(line=pos.line, character=start),
        end=lsp.Position(line=pos.line, character=end),
    )


def _pos_tuple(pos: lsp.Position) -> tuple[int, int]:
    return (pos.line, pos.character)


def _range_key(rng: lsp.Range) -> tuple[int, int, int, int]:
    return (rng.start.line, rng.start.character, rng.end.line, rng.end.character)


def _span_size(rng: lsp.Range) -> tuple[int, int]:
    """Sort key: fewer lines first, then narrower same-line span."""
    dline = rng.end.line - rng.start.line
    dcol = rng.end.character - rng.start.character
    return (dline, dcol)


def _same_range(left: lsp.Range, right: lsp.Range) -> bool:
    return _range_key(left) == _range_key(right)


def _contains_pos(rng: lsp.Range, pos: lsp.Position) -> bool:
    """True when *pos* is in *[start, end)* of *rng*."""
    return _pos_tuple(rng.start) <= _pos_tuple(pos) < _pos_tuple(rng.end)


def _contains_range(outer: lsp.Range, inner: lsp.Range) -> bool:
    return _pos_tuple(outer.start) <= _pos_tuple(inner.start) and _pos_tuple(inner.end) <= _pos_tuple(outer.end)


def _nest_ranges(ranges: list[lsp.Range]) -> list[lsp.Range]:
    """Unique ranges inner-to-outer, each containing the previous."""
    seen: set[tuple[int, int, int, int]] = set()
    uniq: list[lsp.Range] = []
    for rng in ranges:
        key = _range_key(rng)
        if key in seen:
            continue
        seen.add(key)
        uniq.append(rng)
    uniq.sort(key=lambda rng: (_span_size(rng), _range_key(rng)))
    nested: list[lsp.Range] = []
    for rng in uniq:
        if not nested or _contains_range(rng, nested[-1]):
            nested.append(rng)
    return nested


def _to_selection_range(ranges: list[lsp.Range]) -> lsp.SelectionRange | None:
    """Link *ranges* (inner first) as a ``SelectionRange`` parent chain."""
    if not ranges:
        return None
    current = lsp.SelectionRange(range=ranges[-1], parent=None)
    for rng in reversed(ranges[:-1]):
        current = lsp.SelectionRange(range=rng, parent=current)
    return current
