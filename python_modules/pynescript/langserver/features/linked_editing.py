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

"""Linked editing ranges — ``textDocument/linkedEditingRange``.

Public handler: :func:`handle_linked_editing_range`. Returns same-document
ranges for the user identifier under the cursor so the client can drive
live multi-cursor edits. Keywords and builtin namespaces are unlinked
(same set as :mod:`.rename`).
"""

from __future__ import annotations

from typing import Any

from lsprotocol import types as lsp

from pynescript.langserver.features.highlight import collect_ident_hits
from pynescript.langserver.features.highlight import cursor_ident
from pynescript.langserver.features.highlight import is_renamable_ident
from pynescript.langserver.features.highlight import resolve_tree


WORD_PATTERN = r"[A-Za-z_][A-Za-z0-9_]*"


def handle_linked_editing_range(
    params: lsp.LinkedEditingRangeParams,
    source: str | None,
    tree: Any | None = ...,
) -> lsp.LinkedEditingRanges | None:
    """Return same-document ranges for the user ident under the cursor.

    Args:
        params: Client linked-editing params (position).
        source: Document text, or ``None``.
        tree: Pre-parsed AST from the workspace cache. Pass ``None`` when the
            workspace already failed to parse. Omit (default ``...``) to parse
            from *source*.

    Returns:
        :class:`~lsprotocol.types.LinkedEditingRanges` with a Pine ident
        ``word_pattern``, or ``None`` when there is no renamable ident.
    """
    if not source:
        return None

    segment, _start, _end, full = cursor_ident(source, params.position.line, params.position.character)
    if not is_renamable_ident(segment, full):
        return None

    resolved = resolve_tree(source, tree)
    if resolved is None:
        return None

    hits = collect_ident_hits(source, resolved, segment, include_builtin_attrs=False)
    if not hits:
        return None

    return lsp.LinkedEditingRanges(
        ranges=[hit.range for hit in hits],
        word_pattern=WORD_PATTERN,
    )
