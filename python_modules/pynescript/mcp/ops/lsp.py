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

"""Hover, completion, and signature help via existing LSP feature handlers."""

from __future__ import annotations

from typing import Any

from pynescript.mcp.ops.common import load_source
from pynescript.mcp.serialize import fail
from pynescript.mcp.serialize import ok


_URI = "file:///mcp.pine"


def _params_error() -> dict[str, Any]:
    return fail("lsprotocol is required for hover/complete/signature (pip install 'hoox-pyne[mcp]')", kind="io")


def _position(line: int, character: int) -> Any:
    from lsprotocol import types as lsp

    return lsp.Position(line=max(0, line), character=max(0, character))


def hover_source(
    source: str | None = None,
    path: str | None = None,
    *,
    encoding: str = "utf-8",
    line: int = 0,
    character: int = 0,
) -> dict[str, Any]:
    """Markdown hover at a 0-based cursor."""
    try:
        from lsprotocol import types as lsp

        from pynescript.langserver.features.hover import handle_hover
    except ImportError:
        return _params_error()
    loaded = load_source(source, path, encoding=encoding)
    if isinstance(loaded, dict):
        return loaded
    text, label = loaded
    params = lsp.HoverParams(
        text_document=lsp.TextDocumentIdentifier(uri=_URI),
        position=_position(line, character),
    )
    result = handle_hover(params, text)
    if result is None:
        return ok(file=label, found=False, markdown=None)
    contents = result.contents
    markdown = contents.value if hasattr(contents, "value") else str(contents)
    rng = None
    if result.range is not None:
        rng = {
            "start": {"line": result.range.start.line, "character": result.range.start.character},
            "end": {"line": result.range.end.line, "character": result.range.end.character},
        }
    return ok(file=label, found=True, markdown=markdown, range=rng)


def complete_source(
    source: str | None = None,
    path: str | None = None,
    *,
    encoding: str = "utf-8",
    line: int = 0,
    character: int = 0,
    limit: int = 40,
) -> dict[str, Any]:
    """Completion items at a 0-based cursor."""
    try:
        from lsprotocol import types as lsp

        from pynescript.langserver.features.completion import handle_completion
    except ImportError:
        return _params_error()
    loaded = load_source(source, path, encoding=encoding)
    if isinstance(loaded, dict):
        return loaded
    text, label = loaded
    params = lsp.CompletionParams(
        text_document=lsp.TextDocumentIdentifier(uri=_URI),
        position=_position(line, character),
    )
    listing = handle_completion(params, text)
    items = []
    for item in listing.items[: max(1, min(limit, 200))]:
        kind = item.kind
        items.append(
            {
                "label": item.label,
                "detail": item.detail,
                "kind": kind.name if hasattr(kind, "name") else kind,
                "insert_text": item.insert_text,
            }
        )
    return ok(file=label, items=items, is_incomplete=bool(listing.is_incomplete))


def signature_source(
    source: str | None = None,
    path: str | None = None,
    *,
    encoding: str = "utf-8",
    line: int = 0,
    character: int = 0,
) -> dict[str, Any]:
    """Signature help for the call enclosing a 0-based cursor."""
    try:
        from lsprotocol import types as lsp

        from pynescript.langserver.features.signature_help import handle_signature_help
    except ImportError:
        return _params_error()
    loaded = load_source(source, path, encoding=encoding)
    if isinstance(loaded, dict):
        return loaded
    text, label = loaded
    params = lsp.SignatureHelpParams(
        text_document=lsp.TextDocumentIdentifier(uri=_URI),
        position=_position(line, character),
    )
    result = handle_signature_help(params, text)
    if result is None:
        return ok(file=label, found=False)
    signatures = []
    for sig in result.signatures:
        signatures.append(
            {
                "label": sig.label,
                "documentation": getattr(getattr(sig, "documentation", None), "value", None)
                or (sig.documentation if isinstance(sig.documentation, str) else None),
                "parameters": [
                    {"label": p.label if isinstance(p.label, str) else str(p.label)} for p in (sig.parameters or [])
                ],
            }
        )
    return ok(
        file=label,
        found=True,
        active_signature=result.active_signature,
        active_parameter=result.active_parameter,
        signatures=signatures,
    )
