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

"""pygls Language Server for Pine Script.

:class:`PynescriptLanguageServer` owns a :class:`~pynescript.langserver.workspace.Workspace`
(open documents, AST, lint) and registers LSP method handlers in
:meth:`PynescriptLanguageServer.setup_method_handlers`.

**Lifecycle / sync**

- ``initialize`` / ``initialized`` / ``shutdown``
- ``textDocument/didOpen|didChange|didClose|didSave`` — update workspace,
  publish diagnostics
- ``textDocument/diagnostic`` and ``workspace/diagnostic`` — pull diagnostics

**Feature handlers** (delegated to :mod:`pynescript.langserver.features`)

- completion (+ resolve), hover, signatureHelp, definition, references
- documentHighlight, linkedEditingRange, prepareRename, rename (open workspace documents)
- documentSymbol, workspace/symbol
- formatting / rangeFormatting / onTypeFormatting
- inlayHint (+ resolve), semanticTokens/full, semanticTokens/range, foldingRange, selectionRange
- documentColor / colorPresentation (``#hex`` literals)
- documentLink (+ resolve tooltip) (``http(s)`` URLs in comments and string literals)
- codeAction (Convert-to-v6 + W001 QuickFix) + ``pynescript.convertToV6`` executeCommand

Advertised capabilities come from :func:`pynescript.langserver.config.get_server_capabilities`
— only list methods that have implementations here.

Entry: :func:`pynescript.langserver.__main__.main` or construct this class and
call ``start_io()``.
"""

from __future__ import annotations

import asyncio
import logging
import os

from concurrent.futures import ThreadPoolExecutor
from typing import Any

from lsprotocol import types as lsp
from pygls.lsp.server import LanguageServer

from pynescript.__about__ import __version__
from pynescript.langserver import config
from pynescript.langserver.features import completion as completion_feature
from pynescript.langserver.features import convert as convert_feature
from pynescript.langserver.features import definitions as definitions_feature
from pynescript.langserver.features import document_color as document_color_feature
from pynescript.langserver.features import document_link as document_link_feature
from pynescript.langserver.features import folding as folding_feature
from pynescript.langserver.features import formatting as formatting_feature
from pynescript.langserver.features import highlight as highlight_feature
from pynescript.langserver.features import hover as hover_feature
from pynescript.langserver.features import inlay_hints as inlay_hints_feature
from pynescript.langserver.features import linked_editing as linked_editing_feature
from pynescript.langserver.features import references as references_feature
from pynescript.langserver.features import rename as rename_feature
from pynescript.langserver.features import selection_range as selection_range_feature
from pynescript.langserver.features import semantic_tokens as semantic_tokens_feature
from pynescript.langserver.features import signature_help as signature_help_feature
from pynescript.langserver.features import symbols as symbols_feature
from pynescript.langserver.workspace import Workspace


logger = logging.getLogger(__name__)

_DEBOUNCE_MS_DEFAULT = 250

# Sentinel for feature-cache misses (distinct from a cached ``None`` result).
_MISSING = object()


def _debounce_seconds() -> float:
    """Debounce window for diagnostics after ``didChange`` (``PYNESCRIPT_LSP_DEBOUNCE_MS``)."""
    raw = os.environ.get("PYNESCRIPT_LSP_DEBOUNCE_MS", "").strip()
    if raw:
        try:
            return max(0, int(raw)) / 1000
        except ValueError:
            pass
    return _DEBOUNCE_MS_DEFAULT / 1000


class PynescriptLanguageServer(LanguageServer):
    """Pine Script LSP server (pygls :class:`~pygls.lsp.server.LanguageServer` subclass).

    Attributes:
        pine_workspace: In-memory document store keyed by URI. Feature handlers
            read source via :meth:`~pynescript.langserver.workspace.Workspace.get_source`.
    """

    def __init__(self) -> None:
        """Create the server, empty workspace, and register all method handlers."""
        super().__init__(
            name="Pynescript",
            version=__version__,
        )

        self.pine_workspace = Workspace()
        self._pending_diag_tasks: dict[str, asyncio.Task] = {}
        # Single worker serializes parse+lint so typing bursts cannot spawn
        # concurrent parses; keeps one warm thread-local ANTLR engine.
        self._diag_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="pine-lsp-parse")

        self.setup_method_handlers()

    def setup_method_handlers(self) -> None:
        """Register lifecycle, diagnostics, and feature LSP method handlers."""

        @self.feature(lsp.TEXT_DOCUMENT_DID_OPEN)
        def did_open(params: lsp.DidOpenTextDocumentParams) -> None:
            """Handle text document open."""
            uri = params.text_document.uri
            source = params.text_document.text or ""
            version = params.text_document.version

            self.pine_workspace.put_document(uri, source, version)

            doc = self.pine_workspace.get_document(uri)
            if doc:
                lsp_diags = self.pine_workspace._lint_warnings_to_diagnostics(doc)
                self.text_document_publish_diagnostics(lsp.PublishDiagnosticsParams(uri=uri, diagnostics=lsp_diags))

            logger.info(f"Opened document: {uri}")

        @self.feature(lsp.TEXT_DOCUMENT_DID_CHANGE)
        async def did_change(params: lsp.DidChangeTextDocumentParams) -> None:
            """Handle text document changes.

            Applies edits synchronously (cheap) and schedules debounced,
            off-loop parse+lint so the event loop stays responsive during
            typing; diagnostics publish once the parse completes.
            """
            uri = params.text_document.uri
            version = params.text_document.version
            changes = params.content_changes

            # Graceful recovery: some clients send didChange without didOpen
            # (e.g. after a server restart). Treat as an empty open.
            if self.pine_workspace.get_document(uri) is None:
                self.pine_workspace.put_document(uri, "", version)

            doc = self.pine_workspace.update_document(uri, list(changes), version, relint=False)
            self._schedule_diagnostics(uri, doc)

            logger.debug(f"Changed document: {uri} (v{version})")

        @self.feature(lsp.TEXT_DOCUMENT_DID_CLOSE)
        def did_close(params: lsp.DidCloseTextDocumentParams) -> None:
            """Handle text document close."""
            uri = params.text_document.uri
            self._cancel_pending_diagnostics(uri)
            self.pine_workspace.remove_document(uri)
            self.text_document_publish_diagnostics(lsp.PublishDiagnosticsParams(uri=uri, diagnostics=[]))
            logger.info(f"Closed document: {uri}")

        @self.feature(lsp.TEXT_DOCUMENT_DID_SAVE)
        def did_save(params: lsp.DidSaveTextDocumentParams) -> None:
            """Handle text document save.

            Flushes any pending debounced diagnostics so the on-disk state
            is validated immediately.
            """
            uri = params.text_document.uri
            self._cancel_pending_diagnostics(uri)
            doc = self.pine_workspace.get_document(uri)
            if doc:
                self.pine_workspace._parse_and_lint(doc)
                lsp_diags = self.pine_workspace._lint_warnings_to_diagnostics(doc)
                self.text_document_publish_diagnostics(lsp.PublishDiagnosticsParams(uri=uri, diagnostics=lsp_diags))
            logger.info(f"Saved document: {uri}")

        @self.feature(lsp.INITIALIZE)
        def initialize(params: lsp.InitializeParams) -> lsp.InitializeResult:
            """Handle server initialization."""
            logger.info(f"Initialize request from {params.client_info}")

            if params.workspace_folders:
                for folder in params.workspace_folders:
                    logger.info(f"Workspace folder: {folder.uri}")

            capabilities = config.get_server_capabilities()

            return lsp.InitializeResult(
                capabilities=capabilities,
                server_info=lsp.ServerInfo(
                    name="Pynescript Language Server",
                    version=__version__,
                ),
            )

        @self.feature(lsp.INITIALIZED)
        def initialized(params: lsp.InitializedParams) -> None:
            """Handle server initialization complete."""
            logger.info("Server initialized and ready")

        @self.feature(lsp.SHUTDOWN)
        def shutdown(params: Any) -> None:
            """Handle shutdown request."""
            logger.info("Shutdown requested")

        # Note: EXIT is handled by the base class via lsp_exit

        @self.feature(lsp.TEXT_DOCUMENT_DIAGNOSTIC)
        def text_document_diagnostic(
            params: lsp.DocumentDiagnosticParams,
        ) -> lsp.DocumentDiagnosticReport:
            """Handle pull diagnostics (LSP 3.16+)."""
            uri = params.text_document.uri
            doc = self.pine_workspace.get_document(uri)

            if not doc:
                return lsp.RelatedFullDocumentDiagnosticReport(
                    kind=lsp.DocumentDiagnosticReportKind.Full,
                    result_id=None,
                    items=[],
                )

            lsp_diags = self.pine_workspace._lint_warnings_to_diagnostics(doc)

            return lsp.RelatedFullDocumentDiagnosticReport(
                kind=lsp.DocumentDiagnosticReportKind.Full,
                result_id=f"{uri}-{doc.version}",
                items=lsp_diags,
            )

        @self.feature(lsp.WORKSPACE_DIAGNOSTIC)
        def workspace_diagnostics(
            params: lsp.WorkspaceDiagnosticParams,
        ) -> lsp.WorkspaceDiagnosticReport:
            """Handle workspace diagnostics pull."""
            all_diags = self.pine_workspace.get_all_diagnostics()

            items = []
            for doc_uri, diags in all_diags.items():
                items.append(
                    lsp.WorkspaceFullDocumentDiagnosticReport(
                        uri=doc_uri,
                        items=diags,
                        kind=lsp.DocumentDiagnosticReportKind.Full,
                        version=None,
                    )
                )

            return lsp.WorkspaceDiagnosticReport(items=items)

        @self.feature(lsp.WORKSPACE_SYMBOL)
        def workspace_symbol(
            params: lsp.WorkspaceSymbolParams,
        ) -> list[lsp.SymbolInformation]:
            """Handle workspace symbol search."""
            query = params.query.lower() if params.query else ""
            results = []

            for uri, doc in self.pine_workspace.documents.items():
                if doc.ast:
                    # Per-version memo: the symbol walk is identical between edits.
                    symbols = doc.feature_cache.get("symbols", _MISSING)
                    if symbols is _MISSING:
                        symbols = _collect_workspace_symbols(doc, uri)
                        doc.feature_cache["symbols"] = symbols
                    for sym in symbols:
                        if query in sym.name.lower():
                            results.append(sym)

            return results

        @self.feature(lsp.WORKSPACE_EXECUTE_COMMAND)
        def execute_command(params: lsp.ExecuteCommandParams) -> Any:
            """Handle workspace execute command."""
            logger.info(f"Execute command: {params.command}")
            if params.command != convert_feature.COMMAND_CONVERT_V6:
                return None
            args = list(params.arguments or [])
            uri = str(args[0]) if args else ""
            doc = self.pine_workspace.get_document(uri) if uri else None
            source = doc.source if doc else ""
            payload = convert_feature.handle_convert_to_v6(source)
            payload["uri"] = uri
            return payload

        @self.feature(lsp.TEXT_DOCUMENT_CODE_ACTION)
        def code_action(params: lsp.CodeActionParams) -> list[lsp.CodeAction]:
            """Offer Convert-to-v6 and a W001 QuickFix (insert ``//@version=6``)."""
            uri = params.text_document.uri
            doc = self.pine_workspace.get_document(uri)
            source = doc.source if doc else None
            warnings = doc.diagnostics if doc else None
            return convert_feature.handle_code_action(params, source, warnings)

        @self.feature(lsp.TEXT_DOCUMENT_COMPLETION)
        def text_completion(
            params: lsp.CompletionParams,
        ) -> lsp.CompletionList:
            """Handle textDocument/completion request."""
            uri = params.text_document.uri
            doc = self.pine_workspace.get_document(uri)
            if not doc:
                return completion_feature.handle_completion(params, None)
            return completion_feature.handle_completion(params, doc.source, tree=doc.ast)

        @self.feature(lsp.COMPLETION_ITEM_RESOLVE)
        def completion_resolve(
            params: lsp.CompletionItem,
        ) -> lsp.CompletionItem:
            """Handle completionItem/resolve request."""
            return completion_feature.handle_completion_resolve(params)

        @self.feature(lsp.TEXT_DOCUMENT_HOVER)
        def text_hover(
            params: lsp.HoverParams,
        ) -> lsp.Hover | None:
            """Handle textDocument/hover request."""
            uri = params.text_document.uri
            doc = self.pine_workspace.get_document(uri)
            if not doc:
                return hover_feature.handle_hover(params, None)
            return hover_feature.handle_hover(params, doc.source, tree=doc.ast, cache=doc.feature_cache)

        @self.feature(lsp.TEXT_DOCUMENT_SIGNATURE_HELP)
        def text_signature_help(
            params: lsp.SignatureHelpParams,
        ) -> lsp.SignatureHelp | None:
            """Handle textDocument/signatureHelp request."""
            uri = params.text_document.uri
            doc = self.pine_workspace.get_document(uri)
            if not doc:
                return signature_help_feature.handle_signature_help(params, None)
            return signature_help_feature.handle_signature_help(params, doc.source, tree=doc.ast)

        @self.feature(lsp.TEXT_DOCUMENT_DEFINITION)
        def text_definition(
            params: lsp.DefinitionParams,
        ) -> list[lsp.Location] | None:
            """Handle textDocument/definition request."""
            uri = params.text_document.uri
            doc = self.pine_workspace.get_document(uri)
            if not doc:
                return definitions_feature.handle_definition(params, None, uri)
            # Pass workspace AST (or None on parse failure) to skip re-parse.
            return definitions_feature.handle_definition(params, doc.source, uri, tree=doc.ast)

        @self.feature(lsp.TEXT_DOCUMENT_REFERENCES)
        def text_references(
            params: lsp.ReferenceParams,
        ) -> list[lsp.Location]:
            """Handle textDocument/references request."""
            uri = params.text_document.uri
            doc = self.pine_workspace.get_document(uri)
            if not doc:
                return references_feature.handle_references(params, None, uri)
            return references_feature.handle_references(params, doc.source, uri, tree=doc.ast)

        @self.feature(lsp.TEXT_DOCUMENT_DOCUMENT_HIGHLIGHT)
        def text_document_highlight(
            params: lsp.DocumentHighlightParams,
        ) -> list[lsp.DocumentHighlight]:
            """Handle textDocument/documentHighlight request."""
            uri = params.text_document.uri
            doc = self.pine_workspace.get_document(uri)
            if not doc:
                return highlight_feature.handle_document_highlight(params, None)
            return highlight_feature.handle_document_highlight(params, doc.source, tree=doc.ast)

        @self.feature(lsp.TEXT_DOCUMENT_LINKED_EDITING_RANGE)
        def text_linked_editing_range(
            params: lsp.LinkedEditingRangeParams,
        ) -> lsp.LinkedEditingRanges | None:
            """Handle textDocument/linkedEditingRange request."""
            uri = params.text_document.uri
            doc = self.pine_workspace.get_document(uri)
            if not doc:
                return linked_editing_feature.handle_linked_editing_range(params, None)
            return linked_editing_feature.handle_linked_editing_range(params, doc.source, tree=doc.ast)

        @self.feature(lsp.TEXT_DOCUMENT_PREPARE_RENAME)
        def text_prepare_rename(
            params: lsp.PrepareRenameParams,
        ) -> lsp.Range | None:
            """Handle textDocument/prepareRename request."""
            uri = params.text_document.uri
            doc = self.pine_workspace.get_document(uri)
            if not doc:
                return rename_feature.handle_prepare_rename(params, None)
            return rename_feature.handle_prepare_rename(params, doc.source, tree=doc.ast)

        @self.feature(lsp.TEXT_DOCUMENT_RENAME)
        def text_rename(
            params: lsp.RenameParams,
        ) -> lsp.WorkspaceEdit | None:
            """Handle textDocument/rename across currently open documents."""
            uri = params.text_document.uri
            doc = self.pine_workspace.get_document(uri)
            if not doc:
                return rename_feature.handle_rename(params, None, uri)
            return rename_feature.handle_rename(
                params,
                doc.source,
                uri,
                tree=doc.ast,
                workspace=self.pine_workspace,
            )

        @self.feature(lsp.TEXT_DOCUMENT_DOCUMENT_SYMBOL)
        def text_document_symbol(
            params: lsp.DocumentSymbolParams,
        ) -> list[lsp.DocumentSymbol]:
            """Handle textDocument/documentSymbol request."""
            uri = params.text_document.uri
            doc = self.pine_workspace.get_document(uri)
            if not doc:
                return symbols_feature.handle_document_symbols(params, None, uri)
            return symbols_feature.handle_document_symbols(params, doc.source, uri, tree=doc.ast)

        @self.feature(lsp.TEXT_DOCUMENT_FORMATTING)
        def text_formatting(
            params: lsp.DocumentFormattingParams,
        ) -> list[lsp.TextEdit] | None:
            """Handle textDocument/formatting request."""
            uri = params.text_document.uri
            doc = self.pine_workspace.get_document(uri)
            if not doc:
                return formatting_feature.handle_formatting(params, None)
            return formatting_feature.handle_formatting(params, doc.source, tree=doc.ast)

        @self.feature(lsp.TEXT_DOCUMENT_RANGE_FORMATTING)
        def text_range_formatting(
            params: lsp.DocumentRangeFormattingParams,
        ) -> list[lsp.TextEdit] | None:
            """Handle textDocument/rangeFormatting request."""
            uri = params.text_document.uri
            doc = self.pine_workspace.get_document(uri)
            if not doc:
                return formatting_feature.handle_range_formatting(params, None)
            return formatting_feature.handle_range_formatting(params, doc.source, tree=doc.ast)

        @self.feature(lsp.TEXT_DOCUMENT_ON_TYPE_FORMATTING)
        def text_on_type_formatting(
            params: lsp.DocumentOnTypeFormattingParams,
        ) -> list[lsp.TextEdit] | None:
            """Handle textDocument/onTypeFormatting request."""
            uri = params.text_document.uri
            doc = self.pine_workspace.get_document(uri)
            if not doc:
                return formatting_feature.handle_on_type_formatting(params, None)
            return formatting_feature.handle_on_type_formatting(params, doc.source, tree=doc.ast)

        @self.feature(lsp.TEXT_DOCUMENT_INLAY_HINT)
        def text_inlay_hints(
            params: lsp.InlayHintParams,
        ) -> list[lsp.InlayHint] | None:
            """Handle textDocument/inlayHint request."""
            uri = params.text_document.uri
            doc = self.pine_workspace.get_document(uri)
            if not doc:
                return inlay_hints_feature.handle_inlay_hints(params, None)
            # Pure function of (source, tree): memoize per document version.
            cached = doc.feature_cache.get("inlay_hints", _MISSING)
            if cached is not _MISSING:
                return cached
            result = inlay_hints_feature.handle_inlay_hints(params, doc.source, tree=doc.ast)
            doc.feature_cache["inlay_hints"] = result
            return result

        @self.feature(lsp.INLAY_HINT_RESOLVE)
        def inlay_hint_resolve(params: lsp.InlayHint) -> lsp.InlayHint:
            """Handle inlayHint/resolve request (fill textEdits)."""
            return inlay_hints_feature.handle_inlay_hint_resolve(params)

        @self.feature(lsp.TEXT_DOCUMENT_FOLDING_RANGE)
        def text_folding_range(
            params: lsp.FoldingRangeParams,
        ) -> list[lsp.FoldingRange]:
            """Handle textDocument/foldingRange request."""
            uri = params.text_document.uri
            doc = self.pine_workspace.get_document(uri)
            if not doc:
                return folding_feature.handle_folding_range(params, None)
            cached = doc.feature_cache.get("folding_range", _MISSING)
            if cached is not _MISSING:
                return cached
            result = folding_feature.handle_folding_range(params, doc.source, tree=doc.ast)
            doc.feature_cache["folding_range"] = result
            return result

        @self.feature(lsp.TEXT_DOCUMENT_SELECTION_RANGE)
        def text_selection_range(
            params: lsp.SelectionRangeParams,
        ) -> list[lsp.SelectionRange]:
            """Handle textDocument/selectionRange request."""
            uri = params.text_document.uri
            doc = self.pine_workspace.get_document(uri)
            if not doc:
                return selection_range_feature.handle_selection_range(params, None)
            return selection_range_feature.handle_selection_range(params, doc.source, tree=doc.ast)

        @self.feature(lsp.TEXT_DOCUMENT_DOCUMENT_COLOR)
        def text_document_color(
            params: lsp.DocumentColorParams,
        ) -> list[lsp.ColorInformation]:
            """Handle textDocument/documentColor request."""
            uri = params.text_document.uri
            doc = self.pine_workspace.get_document(uri)
            if not doc:
                return document_color_feature.handle_document_color(params, None)
            cached = doc.feature_cache.get("document_color", _MISSING)
            if cached is not _MISSING:
                return cached
            result = document_color_feature.handle_document_color(params, doc.source, tree=doc.ast)
            doc.feature_cache["document_color"] = result
            return result

        @self.feature(lsp.TEXT_DOCUMENT_COLOR_PRESENTATION)
        def text_color_presentation(
            params: lsp.ColorPresentationParams,
        ) -> list[lsp.ColorPresentation]:
            """Handle textDocument/colorPresentation request."""
            uri = params.text_document.uri
            doc = self.pine_workspace.get_document(uri)
            source = doc.source if doc else None
            return document_color_feature.handle_color_presentation(params, source)

        @self.feature(lsp.TEXT_DOCUMENT_DOCUMENT_LINK)
        def text_document_link(
            params: lsp.DocumentLinkParams,
        ) -> list[lsp.DocumentLink]:
            """Handle textDocument/documentLink request."""
            uri = params.text_document.uri
            doc = self.pine_workspace.get_document(uri)
            if not doc:
                return document_link_feature.handle_document_link(params, None)
            cached = doc.feature_cache.get("document_link", _MISSING)
            if cached is not _MISSING:
                return cached
            result = document_link_feature.handle_document_link(params, doc.source, tree=doc.ast)
            doc.feature_cache["document_link"] = result
            return result

        @self.feature(lsp.DOCUMENT_LINK_RESOLVE)
        def document_link_resolve(params: lsp.DocumentLink) -> lsp.DocumentLink:
            """Handle documentLink/resolve request (fill tooltip)."""
            return document_link_feature.handle_document_link_resolve(params)

        @self.feature(lsp.TEXT_DOCUMENT_SEMANTIC_TOKENS_FULL)
        def text_semantic_tokens(
            params: lsp.SemanticTokensParams,
        ) -> lsp.SemanticTokens | None:
            """Handle textDocument/semanticTokens/full request."""
            uri = params.text_document.uri
            doc = self.pine_workspace.get_document(uri)
            if not doc:
                return semantic_tokens_feature.handle_semantic_tokens(params, None)
            # Pure function of (source, tree): memoize per document version.
            cached = doc.feature_cache.get("semantic_tokens", _MISSING)
            if cached is not _MISSING:
                return cached
            result = semantic_tokens_feature.handle_semantic_tokens(params, doc.source, tree=doc.ast)
            doc.feature_cache["semantic_tokens"] = result
            return result

        @self.feature(lsp.TEXT_DOCUMENT_SEMANTIC_TOKENS_RANGE)
        def text_semantic_tokens_range(
            params: lsp.SemanticTokensRangeParams,
        ) -> lsp.SemanticTokens | None:
            """Handle textDocument/semanticTokens/range request."""
            uri = params.text_document.uri
            doc = self.pine_workspace.get_document(uri)
            if not doc:
                return semantic_tokens_feature.handle_semantic_tokens_range(params, None)
            return semantic_tokens_feature.handle_semantic_tokens_range(params, doc.source, tree=doc.ast)

    def _schedule_diagnostics(self, uri: str, doc: Any) -> None:
        """Schedule (or re-schedule) a debounced parse+lint+publish for *uri*."""
        pending = self._pending_diag_tasks.get(uri)
        if pending is not None and not pending.done():
            pending.cancel()
        self._pending_diag_tasks[uri] = asyncio.ensure_future(self._publish_diagnostics_after(uri, doc))

    def _cancel_pending_diagnostics(self, uri: str) -> None:
        """Drop any pending debounced diagnostics task for *uri*."""
        pending = self._pending_diag_tasks.pop(uri, None)
        if pending is not None and not pending.done():
            pending.cancel()

    async def _publish_diagnostics_after(self, uri: str, doc: Any) -> None:
        """Debounce, parse+lint off the event loop, then publish diagnostics."""
        try:
            await asyncio.sleep(_debounce_seconds())
            loop = asyncio.get_running_loop()
            await loop.run_in_executor(self._diag_executor, self.pine_workspace._parse_and_lint, doc)
            lsp_diags = self.pine_workspace._lint_warnings_to_diagnostics(doc)
            self.text_document_publish_diagnostics(lsp.PublishDiagnosticsParams(uri=uri, diagnostics=lsp_diags))
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Debounced diagnostics failed for %s", uri)
        finally:
            if self._pending_diag_tasks.get(uri) is asyncio.current_task():
                self._pending_diag_tasks.pop(uri, None)


def _collect_workspace_symbols(doc: Any, uri: str) -> list[lsp.SymbolInformation]:
    """Collect symbols from a document for workspace symbol search."""
    from pynescript.ast import node as ast

    results = []

    def visit(node: Any) -> None:
        if isinstance(node, ast.FunctionDef):
            results.append(
                lsp.SymbolInformation(
                    name=node.name or "<anonymous>",
                    kind=lsp.SymbolKind.Function,
                    location=lsp.Location(
                        uri=uri,
                        range=lsp.Range(
                            start=lsp.Position(line=max(0, node.lineno - 1), character=0),
                            end=lsp.Position(line=max(0, node.lineno - 1), character=0),
                        ),
                    ),
                )
            )
        elif isinstance(node, ast.TypeDef):
            results.append(
                lsp.SymbolInformation(
                    name=node.name or "<anonymous>",
                    kind=lsp.SymbolKind.Class,
                    location=lsp.Location(
                        uri=uri,
                        range=lsp.Range(
                            start=lsp.Position(line=max(0, node.lineno - 1), character=0),
                            end=lsp.Position(line=max(0, node.lineno - 1), character=0),
                        ),
                    ),
                )
            )
        elif isinstance(node, ast.EnumDef):
            results.append(
                lsp.SymbolInformation(
                    name=node.name or "<anonymous>",
                    kind=lsp.SymbolKind.Enum,
                    location=lsp.Location(
                        uri=uri,
                        range=lsp.Range(
                            start=lsp.Position(line=max(0, node.lineno - 1), character=0),
                            end=lsp.Position(line=max(0, node.lineno - 1), character=0),
                        ),
                    ),
                )
            )
            return
        elif isinstance(node, ast.Assign):
            if isinstance(node.target, ast.Name):
                results.append(
                    lsp.SymbolInformation(
                        name=node.target.id,
                        kind=lsp.SymbolKind.Variable,
                        location=lsp.Location(
                            uri=uri,
                            range=lsp.Range(
                                start=lsp.Position(line=max(0, node.target.lineno - 1), character=0),
                                end=lsp.Position(line=max(0, node.target.lineno - 1), character=0),
                            ),
                        ),
                    )
                )

        for child in getattr(node, "_fields", []):
            child_node = getattr(node, child, None)
            if isinstance(child_node, list):
                for item in child_node:
                    if hasattr(item, "_fields"):
                        visit(item)
            elif hasattr(child_node, "_fields"):
                visit(child_node)

    if hasattr(doc, "ast") and doc.ast:
        visit(doc.ast)

    return results
