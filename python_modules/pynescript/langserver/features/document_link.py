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

"""Document links — ``textDocument/documentLink`` and ``documentLink/resolve``.

Public handlers:

- :func:`handle_document_link` — scan ``http://`` / ``https://`` URLs in
  comments (``//``, ``/* */``, hash ``# …``) and string literals. Each match
  is a :class:`~lsprotocol.types.DocumentLink` whose range is the URL and
  whose ``target`` is that URL. Empty source or parse failure returns ``[]``.
- :func:`handle_document_link_resolve` — fill ``tooltip`` with the URL (or
  keep an existing tooltip). Empty or malformed links are returned unchanged.

Capability: ``resolve_provider=True``.
"""

from __future__ import annotations

import logging
import re

from collections.abc import Iterator
from typing import Any

from lsprotocol import types as lsp

from pynescript.ast import node as ast
from pynescript.ast.helper import parse
from pynescript.ast.helper import walk
from pynescript.langserver.protocol.utils import position_from_offset

from ._ranges import node_to_range as _node_to_range


logger = logging.getLogger(__name__)

_URL_RE = re.compile(r"https?://[^\s<>\"'`*]+")
_TRAILING = ".,;:!?"
_HEX_DIGIT = frozenset("0123456789abcdefABCDEF")
_HASH_SPACE = frozenset(" \t\f")
_QUOTE_LEN = 1


def handle_document_link(
    params: lsp.DocumentLinkParams,
    source: str | None,
    tree: Any | None = ...,
) -> list[lsp.DocumentLink]:
    """Handle textDocument/documentLink request.

    Args:
        params: Client document-link params.
        source: Document text, or ``None``.
        tree: Pre-parsed AST from the workspace cache. Pass ``None`` when the
            workspace already failed to parse (skips a redundant re-parse).
            Omit (default ``...``) to parse from *source*.

    Returns:
        Document links for ``http(s)`` URLs in comments and string literals
        (possibly empty). Never ``None``.
    """
    if tree is ...:
        if not source:
            return []
        try:
            tree = parse(source, filename=params.text_document.uri)
        except Exception as exc:
            logger.debug("document_link: parse failed: %s", exc)
            return []
    if tree is None:
        return []

    results: list[lsp.DocumentLink] = []
    if source:
        results.extend(_links_from_comments(source))
    results.extend(_links_from_strings(tree, source))
    results.sort(key=lambda ln: (ln.range.start.line, ln.range.start.character))
    return results


def handle_document_link_resolve(link: lsp.DocumentLink) -> lsp.DocumentLink:
    """Fill ``tooltip`` with the link ``target`` URL.

    Initial ``textDocument/documentLink`` responses already carry ``target``.
    Resolve copies that URL onto ``tooltip`` so clients can show it on hover.
    An existing tooltip is kept. Empty or malformed links are returned
    unchanged (never raises).

    Args:
        link: The document link to resolve (from the client, possibly with
            ``target`` round-tripped from the initial response).

    Returns:
        The same link with ``tooltip`` set to ``target`` when both were
        missing a tooltip and ``target`` is a non-empty URL string.
    """
    try:
        if getattr(link, "tooltip", None) is not None:
            return link
        target = getattr(link, "target", None)
        if isinstance(target, str) and target:
            link.tooltip = target
        return link
    except Exception:
        return link


def _links_from_comments(source: str) -> list[lsp.DocumentLink]:
    """Collect http(s) links from ``//``, ``/* */``, and hash comments."""
    links: list[lsp.DocumentLink] = []
    for start, end in _comment_spans(source):
        text = source[start:end]
        line, col = position_from_offset(source, start)
        links.extend(_links_in_text(text, line, col))
    return links


def _links_from_strings(tree: Any, source: str | None) -> list[lsp.DocumentLink]:
    """Collect http(s) links from string :class:`~pynescript.ast.node.Constant` nodes."""
    links: list[lsp.DocumentLink] = []
    for node in walk(tree):
        if not isinstance(node, ast.Constant) or not isinstance(node.value, str):
            continue
        node_links = _links_from_string_node(node, source)
        if node_links:
            links.extend(node_links)
    return links


def _links_from_string_node(node: ast.Constant, source: str | None) -> list[lsp.DocumentLink]:
    """URLs inside one string Constant; range covers the URL, not the quotes."""
    lineno = getattr(node, "lineno", None)
    if not lineno:
        return []
    start_line = lineno - 1
    start_col = getattr(node, "col_offset", 0) or 0
    if source:
        text = _node_source(source, node)
        if text is None:
            text = node.value
            start_col += _QUOTE_LEN
    else:
        text = node.value
        start_col += _QUOTE_LEN
    return _links_in_text(text, start_line, start_col)


def _links_in_text(text: str, start_line: int, start_col: int) -> list[lsp.DocumentLink]:
    """Find http(s) URLs in *text* whose origin is (*start_line*, *start_col*)."""
    links: list[lsp.DocumentLink] = []
    for match in _URL_RE.finditer(text):
        url = match.group(0).rstrip(_TRAILING)
        if not url:
            continue
        abs_start = match.start()
        abs_end = abs_start + len(url)
        sl, sc = _offset_to_line_col(text, abs_start, start_line, start_col)
        el, ec = _offset_to_line_col(text, abs_end, start_line, start_col)
        links.append(
            lsp.DocumentLink(
                range=lsp.Range(
                    start=lsp.Position(line=sl, character=sc),
                    end=lsp.Position(line=el, character=ec),
                ),
                target=url,
            )
        )
    return links


def _offset_to_line_col(text: str, offset: int, base_line: int, base_col: int) -> tuple[int, int]:
    """Map an offset in *text* to LSP line/col relative to the region's origin."""
    prefix = text[:offset]
    newlines = prefix.count("\n")
    if newlines == 0:
        return base_line, base_col + offset
    last = prefix.rfind("\n")
    return base_line + newlines, offset - last - 1


def _comment_spans(source: str) -> Iterator[tuple[int, int]]:
    """Yield ``[start, end)`` offsets of line, block, and hash comments."""
    i = 0
    n = len(source)
    while i < n:
        ch = source[i]
        if ch in "\"'":
            i = _skip_string(source, i)
            continue
        if ch == "/" and i + 1 < n:
            nxt = source[i + 1]
            if nxt == "/":
                end = source.find("\n", i)
                yield i, n if end < 0 else end
                i = n if end < 0 else end
                continue
            if nxt == "*":
                end = source.find("*/", i + 2)
                yield i, n if end < 0 else end + 2
                i = n if end < 0 else end + 2
                continue
        if ch == "#" and _is_hash_comment(source, i):
            end = source.find("\n", i)
            yield i, n if end < 0 else end
            i = n if end < 0 else end
            continue
        i += 1


def _is_hash_comment(source: str, i: int) -> bool:
    """True when ``source[i]`` starts a HASH_COMMENT (not a ``#rgb`` color)."""
    if i + 1 >= len(source):
        return False
    nxt = source[i + 1]
    if nxt in _HASH_SPACE:
        return True
    if nxt in "\r\n":
        return False
    return nxt not in _HEX_DIGIT


def _skip_string(source: str, i: int) -> int:
    """Return the index after the string literal starting at *i*."""
    n = len(source)
    quote = source[i]
    triple = quote * 3
    if i + 2 < n and source[i : i + 3] == triple:
        close = source.find(triple, i + 3)
        return n if close < 0 else close + 3
    i += 1
    while i < n:
        ch = source[i]
        if ch == "\\":
            i += 2
            continue
        if ch in (quote, "\n"):
            return i + 1 if ch == quote else i
        i += 1
    return n


def _node_source(source: str, node: Any) -> str | None:
    """Slice *source* by AST location attributes, or ``None`` if missing."""
    rng = _node_to_range(node)
    if rng is None:
        return None
    lines = source.split("\n")
    start, end = rng.start, rng.end
    if start.line < 0 or start.line >= len(lines):
        return None
    if start.line == end.line:
        return lines[start.line][start.character : end.character]
    parts = [lines[start.line][start.character :]]
    for line_i in range(start.line + 1, min(end.line, len(lines))):
        parts.append(lines[line_i])
    if 0 <= end.line < len(lines):
        parts.append(lines[end.line][: end.character])
    return "\n".join(parts)
