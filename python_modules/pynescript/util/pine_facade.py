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

"""Download TradingView builtin Pine templates via pine-facade.

Call from Python::

    from pynescript.util.pine_facade import download_builtin_scripts

    download_builtin_scripts("tests/data/builtin_scripts", confirm=False)

Or from the repo root (Rich catalog is printed *before* any GET /get)::

    python -m pynescript.util.pine_facade --list
    python -m pynescript.util.pine_facade --yes
    pyne download-builtins --list
    pyne download-builtins --yes
"""

from __future__ import annotations

import argparse
import os
import pathlib
import sys
import threading
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import as_completed
from dataclasses import dataclass
from html import unescape
from typing import Any
from urllib.parse import quote

import requests  # type: ignore[import-untyped]


FACADE_URL = "https://pine-facade.tradingview.com"
DEFAULT_DEST = pathlib.Path("tests/data/builtin_scripts")
SCRIPT_NAME_REPLACE = {
    " ": "_",
    "-": "_",
    "/": "_",
    "'": "",
    "’": "",
    '"': "",
}

# PYNE volt pack (same hex as scripts/showcase.py / __main__.py)
_PYNE_ACCENT = "#B7EF09"
_PYNE_ACCENT_DIM = "#76A000"
_PYNE_FG = "#EFEFE8"
_PYNE_MUTED = "#CBCCBD"
_PYNE_BORDER = "#58594C"
_PYNE_FAIL = "#E7000B"
_PYNE_OK = "#B7EF09"
_PYNE_WARN = "#EBA941"

_THREAD_LOCAL = threading.local()
_THREAD_SESSIONS: set[requests.Session] = set()
_THREAD_SESSIONS_LOCK = threading.Lock()

_KIND_ALIAS = {
    "study": "indicator",
    "studies": "indicator",
}


@dataclass(frozen=True, slots=True)
class DownloadResult:
    """One template write attempt."""

    name: str
    status: str  # saved | skipped | failed
    filename: str = ""
    error: str | None = None


def _has_rich() -> bool:
    try:
        import rich  # noqa: F401

        return True
    except ImportError:
        return False


def _use_rich(*, force: bool = False, plain: bool = False) -> bool:
    if plain:
        return False
    if not _has_rich():
        return False
    if force:
        return True
    if os.environ.get("NO_COLOR"):
        return False
    return bool(sys.stdout.isatty())


def _console(*, force: bool = False, plain: bool = False) -> Any | None:
    if not _use_rich(force=force, plain=plain):
        return None
    from rich.console import Console
    from rich.theme import Theme

    return Console(
        force_terminal=True if force else None,
        color_system="truecolor" if force else "auto",
        theme=Theme(
            {
                "pyne.accent": f"bold {_PYNE_ACCENT}",
                "pyne.accent_dim": _PYNE_ACCENT_DIM,
                "pyne.fg": _PYNE_FG,
                "pyne.muted": _PYNE_MUTED,
                "pyne.ok": f"bold {_PYNE_OK}",
                "pyne.fail": f"bold {_PYNE_FAIL}",
                "pyne.warn": f"bold {_PYNE_WARN}",
                "pyne.border": _PYNE_BORDER,
            }
        ),
    )


def _register_thread_session(session: requests.Session) -> requests.Session:
    with _THREAD_SESSIONS_LOCK:
        _THREAD_SESSIONS.add(session)
    return session


def _get_thread_session() -> requests.Session:
    session = getattr(_THREAD_LOCAL, "session", None)
    if session is None:
        session = _register_thread_session(requests.Session())
        _THREAD_LOCAL.session = session
    return session


def _close_thread_sessions() -> None:
    with _THREAD_SESSIONS_LOCK:
        sessions = list(_THREAD_SESSIONS)
        _THREAD_SESSIONS.clear()
    for session in sessions:
        session.close()


def _normalize_filename(script_name: str, mapping: dict[str, str] | None = None) -> str:
    mapping = mapping if mapping is not None else SCRIPT_NAME_REPLACE
    script_name_prefix = script_name.lower()
    for from_pattern, replace_to in mapping.items():
        script_name_prefix = script_name_prefix.replace(from_pattern, replace_to)
    return f"{script_name_prefix}.pine"


def _session_provider_factory(base_session: requests.Session) -> Callable[[], requests.Session]:
    def _provider() -> requests.Session:
        return base_session

    return _provider


def _meta_name(script_meta: dict[str, Any]) -> str:
    raw = str(script_meta.get("scriptName") or script_meta.get("name") or "unnamed")
    return unescape(raw)


def _meta_id(script_meta: dict[str, Any]) -> str:
    return str(script_meta.get("scriptIdPart") or script_meta.get("scriptId") or "")


def _pretty_version(raw: Any) -> str:
    text = str(raw or "")
    if text.endswith(".0"):
        return text[:-2]
    return text


def _meta_version(script_meta: dict[str, Any]) -> str:
    return _pretty_version(script_meta.get("version"))


def _canonical_kind(raw: Any) -> str:
    kind = str(raw or "").strip().lower()
    return _KIND_ALIAS.get(kind, kind)


def _meta_kind(script_meta: dict[str, Any], name: str) -> str:
    extra = script_meta.get("extra")
    if isinstance(extra, dict):
        for key in ("kind", "type", "scriptType"):
            value = extra.get(key)
            if value:
                return _canonical_kind(value)
    for key in ("kind", "type", "scriptType"):
        value = script_meta.get(key)
        if value:
            return _canonical_kind(value)
    lowered = name.lower()
    if "strateg" in lowered:
        return "strategy"
    if "library" in lowered:
        return "library"
    return "indicator"


def _meta_extra(script_meta: dict[str, Any]) -> dict[str, Any]:
    extra = script_meta.get("extra")
    return extra if isinstance(extra, dict) else {}


def _meta_short(script_meta: dict[str, Any]) -> str:
    return str(_meta_extra(script_meta).get("shortDescription") or "")


def _meta_plots(script_meta: dict[str, Any]) -> str:
    stats = _meta_extra(script_meta).get("stats")
    if isinstance(stats, dict) and stats.get("plot") is not None:
        return str(stats["plot"])
    return ""


def _kind_markup(kind: str) -> str:
    if kind == "strategy":
        return "[pyne.warn]strategy[/]"
    if kind == "library":
        return "[pyne.muted]library[/]"
    return f"[pyne.accent]{kind}[/]"


def _existing_pines(script_dir: pathlib.Path) -> set[str]:
    if not script_dir.is_dir():
        return set()
    return {p.name for p in script_dir.glob("*.pine") if p.is_file()}


def assign_filenames(
    script_list: list[dict[str, Any]],
    mapping: dict[str, str] | None = None,
) -> list[tuple[dict[str, Any], str]]:
    """Give each catalog entry a unique ``*.pine`` name (version suffix on clashes)."""
    mapping = mapping if mapping is not None else SCRIPT_NAME_REPLACE
    used: set[str] = set()
    assigned: list[tuple[dict[str, Any], str]] = []
    for meta in script_list:
        name = _meta_name(meta)
        filename = _normalize_filename(name, mapping)
        if filename in used:
            stem = filename[:-5] if filename.endswith(".pine") else filename
            ver = _meta_version(meta)
            filename = f"{stem}_v{ver}.pine" if ver else f"{stem}_2.pine"
            n = 2
            while filename in used:
                filename = f"{stem}_{n}.pine"
                n += 1
        used.add(filename)
        assigned.append((meta, filename))
    return assigned


def _download_script(
    script_meta: dict[str, Any],
    script_dir: pathlib.Path,
    encoding: str,
    filename: str,
    session_provider: Callable[[], requests.Session],
    skip_existing: bool,
) -> DownloadResult:
    script_name = _meta_name(script_meta)
    dest = script_dir / filename
    if skip_existing and dest.exists():
        return DownloadResult(script_name, "skipped", filename=filename)

    script_id_part = _meta_id(script_meta)
    script_version = _meta_version(script_meta)
    try:
        session = session_provider()
        script = get_script(script_id_part, script_version, session=session)
        script_source = script["source"]
        dest.write_text(script_source, encoding=encoding)
    except Exception as exc:
        return DownloadResult(script_name, "failed", filename=filename, error=str(exc)[:240])
    return DownloadResult(script_name, "saved", filename=filename)


def list_builtin_scripts(session: requests.Session | None = None):
    url = FACADE_URL
    path = "/pine-facade/list/"
    params = {"filter": "template"}
    requester = session or requests
    response = requester.get(url + path, params=params, timeout=60)
    response.raise_for_status()
    result = response.json()
    return result


def get_script(script_id_part, version, session: requests.Session | None = None):
    url = FACADE_URL
    path = f"/pine-facade/get/{quote(str(script_id_part))}/{version}"
    params = {"no_4xx": "false"}
    requester = session or requests
    response = requester.get(url + path, params=params, timeout=60)
    response.raise_for_status()
    result = response.json()
    return result


def _plain_catalog(
    script_list: list[dict[str, Any]],
    script_dir: pathlib.Path,
    worker_count: int,
    existing: set[str],
) -> None:
    total = len(script_list)
    kinds: dict[str, int] = {}
    already = 0
    print("◆ PYNE  pine facade  builtin templates")
    print(f"  source   {FACADE_URL}")
    print(f"  dest     {script_dir}")
    print(f"  workers  {worker_count}")
    print()
    print(f"{'#':>3}  {'kind':<10}  {'ver':<5}  {'plots':>5}  name")
    assigned_names = [fn for _meta, fn in assign_filenames(script_list)]
    for i, meta in enumerate(script_list, 1):
        name = _meta_name(meta)
        kind = _meta_kind(meta, name)
        kinds[kind] = kinds.get(kind, 0) + 1
        on_disk = assigned_names[i - 1] in existing
        if on_disk:
            already += 1
        mark = "*" if on_disk else " "
        plots = _meta_plots(meta) or "-"
        print(f"{i:3d}{mark} {kind:<10}  {_meta_version(meta):<5}  {plots:>5}  {name}")
    kind_bits = "  ".join(f"{k} {n}" for k, n in sorted(kinds.items()))
    print()
    print(f"  catalog  {total} templates    {kind_bits}")
    print(f"  on disk  {already} already saved    {total - already} to fetch")
    print()


def render_catalog(
    script_list: list[dict[str, Any]],
    script_dir: pathlib.Path,
    worker_count: int,
    existing: set[str] | None = None,
    *,
    console: Any | None = None,
    dry_run: bool = False,
) -> None:
    """Print the template catalog *before* any per-script download starts."""
    existing = existing if existing is not None else _existing_pines(script_dir)
    if console is None:
        _plain_catalog(script_list, script_dir, worker_count, existing)
        return

    from rich import box
    from rich.console import Group
    from rich.panel import Panel
    from rich.table import Table
    from rich.text import Text

    total = len(script_list)
    already = 0
    kinds: dict[str, int] = {}
    overlays = 0
    rows: list[tuple[str, str, str, str, str, str, bool]] = []
    assigned_names = [fn for _meta, fn in assign_filenames(script_list)]
    for i, meta in enumerate(script_list, 1):
        name = _meta_name(meta)
        kind = _meta_kind(meta, name)
        kinds[kind] = kinds.get(kind, 0) + 1
        extra = _meta_extra(meta)
        if extra.get("is_price_study"):
            overlays += 1
        filename = assigned_names[i - 1]
        on_disk = filename in existing
        if on_disk:
            already += 1
        rows.append(
            (
                str(i),
                kind,
                _meta_version(meta),
                name,
                _meta_short(meta),
                _meta_plots(meta),
                on_disk,
            )
        )

    title = Text()
    title.append("◆ PYNE", style="pyne.accent")
    title.append("  pine facade", style="pyne.fg")
    title.append("  builtin templates", style="pyne.muted")
    if dry_run:
        title.append("  · dry-run", style="pyne.warn")

    meta_grid = Table.grid(padding=(0, 2))
    meta_grid.add_column(style="pyne.muted", min_width=9)
    meta_grid.add_column(style="pyne.fg")
    meta_grid.add_row("source", FACADE_URL)
    meta_grid.add_row("dest", str(script_dir))
    meta_grid.add_row("workers", str(worker_count))
    kind_bits = "  ".join(f"{k} {n}" for k, n in sorted(kinds.items())) or "—"
    meta_grid.add_row("catalog", f"{total} templates    {kind_bits}")
    meta_grid.add_row("layout", f"{overlays} overlay    {total - overlays} pane")
    meta_grid.add_row("on disk", f"{already} already saved    {total - already} to fetch")

    table = Table(
        box=box.SIMPLE_HEAD,
        header_style="pyne.accent",
        border_style="pyne.border",
        show_edge=False,
        pad_edge=False,
        expand=True,
        padding=(0, 1),
    )
    table.add_column("#", style="pyne.muted", justify="right", width=4, no_wrap=True)
    table.add_column("kind", width=10, no_wrap=True)
    table.add_column("ver", style="pyne.muted", width=4, no_wrap=True, justify="right")
    table.add_column("name", style="pyne.fg", overflow="ellipsis", ratio=2, no_wrap=True)
    table.add_column("short", style="pyne.muted", overflow="ellipsis", ratio=1, no_wrap=True)
    table.add_column("plots", style="pyne.muted", width=5, justify="right", no_wrap=True)
    table.add_column("disk", style="pyne.muted", width=4, justify="center", no_wrap=True)

    for idx, kind, ver, name, short, plots, on_disk in rows:
        disk = "[pyne.ok]yes[/]" if on_disk else "[pyne.muted]—[/]"
        table.add_row(idx, _kind_markup(kind), ver, name, short, plots, disk)

    console.print(
        Panel(
            Group(title, Text(""), meta_grid, Text(""), table),
            border_style="pyne.border",
            padding=(1, 2),
        )
    )


def _render_summary(
    results: list[DownloadResult],
    script_dir: pathlib.Path,
    elapsed_s: float,
    *,
    console: Any | None = None,
) -> None:
    saved = sum(1 for r in results if r.status == "saved")
    skipped = sum(1 for r in results if r.status == "skipped")
    failed = [r for r in results if r.status == "failed"]
    line = f"{saved} saved  {skipped} skipped  {len(failed)} failed  {elapsed_s:.1f}s  {script_dir}"
    if console is not None:
        style = "pyne.fail" if failed else "pyne.ok"
        mark = "✘" if failed else "✔"
        console.print(f"[{style}]{mark}[/{style}] {line}")
        for item in failed[:12]:
            console.print(f"  [pyne.fail]✘[/] {item.name}  [pyne.muted]{item.error}[/]")
        if len(failed) > 12:
            console.print(f"  [pyne.muted]… and {len(failed) - 12} more failures[/]")
        return
    prefix = "FAIL" if failed else "OK"
    print(f"{prefix}: {line}")
    for item in failed[:12]:
        print(f"  FAIL {item.name}: {item.error}")


def download_builtin_scripts(
    script_dir,
    encoding=None,
    max_workers: int | None = None,
    *,
    dry_run: bool = False,
    confirm: bool = False,
    skip_existing: bool = False,
    limit: int | None = None,
    plain: bool = False,
    force_rich: bool = False,
    console: Any | None = None,
) -> dict[str, Any]:
    """List the pine-facade catalog, preview it, then download each template.

    The catalog table is always rendered *before* the first ``/pine-facade/get``
    request. Pass ``dry_run=True`` to stop after the preview.
    """
    script_dir = pathlib.Path(script_dir)
    encoding = encoding or "utf-8"
    if console is None:
        console = _console(force=force_rich, plain=plain)

    script_dir.mkdir(parents=True, exist_ok=True)

    with requests.Session() as listing_session:
        script_list = list(list_builtin_scripts(session=listing_session) or [])

    if limit is not None:
        script_list = script_list[: max(0, limit)]

    existing = _existing_pines(script_dir)
    total_scripts = len(script_list)
    worker_count = max_workers or min(8, total_scripts or 1)
    worker_count = max(1, worker_count)

    render_catalog(
        script_list,
        script_dir,
        worker_count,
        existing,
        console=console,
        dry_run=dry_run,
    )

    empty: dict[str, Any] = {
        "dest": str(script_dir),
        "total": total_scripts,
        "saved": 0,
        "failed": 0,
        "skipped": 0,
        "elapsed_s": 0.0,
        "failures": [],
    }
    if not script_list:
        if console is not None:
            console.print("[pyne.warn]![/] catalog is empty — nothing to download")
        else:
            print("WARN: catalog is empty — nothing to download")
        return empty

    if dry_run:
        if console is not None:
            console.print("[pyne.muted]dry-run — no templates fetched[/]")
        else:
            print("dry-run — no templates fetched")
        return empty

    if confirm and sys.stdin.isatty():
        proceed = True
        if console is not None:
            from rich.prompt import Confirm

            proceed = Confirm.ask(
                f"[pyne.accent]Download {total_scripts} templates?[/]",
                default=True,
            )
        else:
            answer = input(f"Download {total_scripts} templates? [Y/n] ").strip().lower()
            proceed = answer in {"", "y", "yes"}
        if not proceed:
            if console is not None:
                console.print("[pyne.muted]cancelled[/]")
            else:
                print("cancelled")
            return empty

    started = time.perf_counter()
    results: list[DownloadResult] = []

    if console is not None:
        from rich.progress import BarColumn
        from rich.progress import MofNCompleteColumn
        from rich.progress import Progress
        from rich.progress import SpinnerColumn
        from rich.progress import TextColumn
        from rich.progress import TimeElapsedColumn
        from rich.progress import TimeRemainingColumn
        from rich.table import Column

        progress_cm: Any = Progress(
            SpinnerColumn(style=f"bold {_PYNE_ACCENT}"),
            TextColumn("[pyne.accent]download[/]"),
            BarColumn(
                bar_width=28,
                style=_PYNE_BORDER,
                complete_style=_PYNE_ACCENT,
                finished_style=_PYNE_OK,
                pulse_style=_PYNE_ACCENT_DIM,
            ),
            MofNCompleteColumn(),
            TextColumn(
                "[pyne.muted]{task.fields[script]}",
                table_column=Column(no_wrap=True, overflow="ellipsis", min_width=18, ratio=1),
            ),
            TimeElapsedColumn(),
            TimeRemainingColumn(),
            console=console,
            transient=False,
        )
    else:
        import tqdm  # type: ignore[import-untyped]

        progress_cm = tqdm.tqdm(total=total_scripts, desc="Downloading scripts", unit="script")

    def _on_result(result: DownloadResult) -> None:
        results.append(result)
        if console is not None:
            progress.update(task_id, advance=1, script=result.name)
        else:
            progress.set_postfix_str(result.name, refresh=False)
            progress.update(1)

    with progress_cm as progress:
        task_id = None
        if console is not None:
            task_id = progress.add_task("download", total=total_scripts, script="")

        jobs = assign_filenames(script_list)
        if worker_count == 1:
            with requests.Session() as session:
                session_provider = _session_provider_factory(session)
                for script_meta, filename in jobs:
                    _on_result(
                        _download_script(
                            script_meta,
                            script_dir,
                            encoding,
                            filename,
                            session_provider,
                            skip_existing,
                        )
                    )
        else:
            with ThreadPoolExecutor(max_workers=worker_count) as executor:
                futures = [
                    executor.submit(
                        _download_script,
                        script_meta,
                        script_dir,
                        encoding,
                        filename,
                        _get_thread_session,
                        skip_existing,
                    )
                    for script_meta, filename in jobs
                ]
                try:
                    for future in as_completed(futures):
                        _on_result(future.result())
                except Exception:
                    for future in futures:
                        future.cancel()
                    raise
                finally:
                    _close_thread_sessions()

    elapsed_s = time.perf_counter() - started
    _render_summary(results, script_dir, elapsed_s, console=console)
    failures = [{"name": r.name, "error": r.error} for r in results if r.status == "failed"]
    return {
        "dest": str(script_dir),
        "total": total_scripts,
        "saved": sum(1 for r in results if r.status == "saved"),
        "failed": len(failures),
        "skipped": sum(1 for r in results if r.status == "skipped"),
        "elapsed_s": round(elapsed_s, 3),
        "failures": failures,
    }


def _build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m pynescript.util.pine_facade",
        description="Download TradingView builtin Pine templates (Rich catalog first).",
    )
    parser.add_argument(
        "dest",
        nargs="?",
        default=str(DEFAULT_DEST),
        help=f"Output directory (default: {DEFAULT_DEST})",
    )
    parser.add_argument("--workers", type=int, default=None, help="Parallel GET workers (default: 8).")
    parser.add_argument("--encoding", default="utf-8")
    parser.add_argument("--list", "--dry-run", dest="dry_run", action="store_true", help="Preview catalog only.")
    parser.add_argument("--yes", action="store_true", help="Skip the confirm prompt.")
    parser.add_argument("--skip-existing", action="store_true", help="Do not overwrite files already on disk.")
    parser.add_argument("--limit", type=int, default=None, help="Download only the first N templates.")
    parser.add_argument("--plain", action="store_true", help="Disable Rich (tqdm / ASCII).")
    parser.add_argument("--rich", dest="force_rich", action="store_true", help="Force Rich even when stdout is not a TTY.")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _build_arg_parser().parse_args(argv)
    summary = download_builtin_scripts(
        args.dest,
        encoding=args.encoding,
        max_workers=args.workers,
        dry_run=args.dry_run,
        confirm=not args.yes,
        skip_existing=args.skip_existing,
        limit=args.limit,
        plain=args.plain,
        force_rich=args.force_rich,
    )
    return 1 if summary.get("failed") else 0


if __name__ == "__main__":
    raise SystemExit(main())
