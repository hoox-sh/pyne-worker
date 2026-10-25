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

"""Shared AST-node → ``lsp.Range`` conversion for language-server features.

Several features need the full source span of an AST node (document colours,
selection ranges, document links, symbols). They previously carried their own
copy of this function; keep the single implementation here so a span-handling
fix lands everywhere at once.
"""

from __future__ import annotations

from typing import Any

from lsprotocol import types as lsp


def node_to_range(node: Any) -> lsp.Range | None:
    """Convert an AST node's source span to an ``lsp.Range``.

    Returns ``None`` when the node carries no line information, so callers that
    need a range can fall back (``symbols``) and callers that tolerate missing
    spans can skip the node.

    When ``end_col_offset`` is absent the end is widened to the end of the line
    (``1 << 31``) or to the next character on the same line, so a client never
    sees a range whose end precedes its start.
    """
    lineno = getattr(node, "lineno", None)
    if not lineno:
        return None
    col_offset = getattr(node, "col_offset", 0) or 0
    end_lineno = getattr(node, "end_lineno", lineno) or lineno
    end_col_offset = getattr(node, "end_col_offset", None)
    if end_col_offset is None:
        end_col_offset = col_offset + 1 if end_lineno == lineno else 1 << 31
    return lsp.Range(
        start=lsp.Position(line=max(0, lineno - 1), character=max(0, col_offset)),
        end=lsp.Position(line=max(0, end_lineno - 1), character=max(0, end_col_offset)),
    )
