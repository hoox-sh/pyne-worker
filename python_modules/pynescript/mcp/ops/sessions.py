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

"""Pooled live-session inspector as an MCP op (read-only, no MCP SDK needed).

Pooled script sessions resume by content fingerprint, blind to the bars
underneath: after a repair / replace / purge the store window can move past
a session's birth anchor, and every resubscribe resumes the pre-repair
window (stale overlays that survive reloads). ``pyne_sessions`` reports each
session's binding anchor against the store's current window so a zombie is
one read away:

- ``lag_bars`` — store newest minus the session anchor, in bar steps.
- ``suspect`` — anchor provably outside the store window (older than the
  store oldest, or newer than the store newest after a purge without
  cascade). Unbound sessions (plain ``/session`` runs) carry no anchor and
  are never suspects.
- ``resident`` — in this backend process's memory (``true``) or journaled
  by another worker and visible only via the session log (``false``).

The MCP server runs in its own process, so it never shares the backend's
memory: point ``PYNE_DATA_DIR`` at the backend's data dir for the store
window and ``PYNE_SESSION_DB`` (or the shared ``/data`` volume) at its
session journal. Without a backend on the path the tool fails cleanly.
"""

from __future__ import annotations

import os
import sys
import time

from typing import Any

from pynescript.mcp.serialize import fail
from pynescript.mcp.serialize import jsonable
from pynescript.mcp.serialize import ok


def _backend() -> tuple[Any, Any]:
    """Import the backend store modules (dev checkout on any cwd).

    ``backend`` ships beside ``src`` in the repo, not inside the installed
    distribution, so a bare ``pip install hoox-pyne`` cannot see it:
    resolve the checkout root from the installed ``pynescript`` package
    (editable installs land here) before importing.
    """
    try:
        import backend.datastore.series as series_store
        import backend.middleware.sessions as sessions_mod

        return series_store, sessions_mod
    except ImportError:
        pass
    try:
        import pynescript
    except ImportError as exc:
        raise ImportError(f"pynescript is not importable: {exc}") from exc
    root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(pynescript.__file__))))
    if os.path.isdir(os.path.join(root, "backend")) and root not in sys.path:
        sys.path.insert(0, root)
    import backend.datastore.series as series_store
    import backend.middleware.sessions as sessions_mod

    return series_store, sessions_mod


def _as_int(value: Any) -> int | None:
    """Coerce journal/store timestamps that may arrive as str/float."""
    if value is None or isinstance(value, bool):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _parse_key(series_store: Any, key_text: str) -> Any | None:
    """``exchange|market|SYMBOL|interval`` → SeriesKey, or ``None``."""
    try:
        parts = (key_text or "").split("|")
        if len(parts) != 4:
            return None
        exchange, market, symbol, interval = parts
        return series_store.make_key(exchange, symbol, interval, market)
    except Exception:
        return None


def _journal_rows(sessions_mod: Any, resident_ids: set[str]) -> list[dict[str, Any]]:
    """Sessions journaled by another worker (not in this process's memory)."""
    try:
        from backend.datastore import session_log as session_log_mod
    except ImportError:
        return []
    try:
        db_path = sessions_mod.SESSIONS._db_path or session_log_mod.session_db_path()
    except Exception:
        return []
    if not db_path or not os.path.isfile(db_path):
        return []
    out: list[dict[str, Any]] = []
    for record in session_log_mod.list_meta(db_path):
        sid = str(record.get("id"))
        if sid in resident_ids:
            continue
        binding = record.get("binding") or {}
        try:
            idle = max(0.0, time.time() - float(record.get("updated_at") or 0.0))
        except (TypeError, ValueError):
            idle = 0.0
        out.append(
            {
                "session_id": sid,
                "resident": False,
                "pooled": None,
                "fingerprint": None,
                "refcount": 0,
                "key": binding.get("key") if isinstance(binding, dict) else None,
                "anchor_time": binding.get("last_time") if isinstance(binding, dict) else None,
                "idle_seconds": round(idle, 3),
                "journal_bars": record.get("bar_count"),
            }
        )
    return out


def _enrich(series_store: Any, store: Any, row: dict[str, Any]) -> dict[str, Any]:
    entry: dict[str, Any] = dict(row)
    entry["lag_bars"] = None
    entry["store_oldest"] = None
    entry["store_newest"] = None
    entry["store_count"] = None
    entry["suspect"] = False
    entry["why"] = None
    key_text = row.get("key")
    if not isinstance(key_text, str):
        return entry
    series_key = _parse_key(series_store, key_text)
    if series_key is None:
        return entry
    try:
        status = store.status(series_key)
    except Exception:
        status = None
    if not status:
        return entry
    oldest = _as_int(status.get("oldest"))
    newest = _as_int(status.get("newest"))
    entry["store_oldest"] = oldest
    entry["store_newest"] = newest
    entry["store_count"] = status.get("count")
    anchor = _as_int(row.get("anchor_time"))
    try:
        step = int(series_key.interval_seconds) or 0
    except Exception:
        step = 0
    if anchor is not None and newest is not None and oldest is not None and step > 0:
        entry["lag_bars"] = max(0, (newest - anchor) // step)
        if anchor < oldest:
            entry["suspect"] = True
            entry["why"] = "anchor older than store oldest: session window left behind"
        elif anchor > newest:
            entry["suspect"] = True
            entry["why"] = "anchor newer than store newest: store replaced under the session"
    return entry


def inspect_sessions(key: str | None = None) -> dict[str, Any]:
    """Snapshot live sessions with anchor-vs-store-window bookkeeping.

    ``key`` optionally filters to one ``exchange|market|SYMBOL|interval``
    series. Read-only: never creates, drops, or touches a session.
    """
    try:
        series_store, sessions_mod = _backend()
    except ImportError as exc:
        return fail(f"backend is not importable here: {exc}", kind="io")
    try:
        store = series_store.get_store()
    except Exception as exc:
        return fail(f"series store is not available: {exc}", kind="io")
    try:
        resident = sessions_mod.SESSIONS.describe_all()
    except Exception as exc:
        return fail(f"session store is not readable: {exc}", kind="io")
    for row in resident:
        row["resident"] = True
    resident_ids = {str(r.get("session_id")) for r in resident}
    try:
        journaled = _journal_rows(sessions_mod, resident_ids)
    except Exception:
        journaled = []
    rows: list[dict[str, Any]] = []
    for row in resident + journaled:
        if key is not None and row.get("key") != key:
            continue
        rows.append(_enrich(series_store, store, row))
    suspects = sum(1 for r in rows if r.get("suspect"))
    return jsonable(
        ok(
            sessions=rows,
            count=len(rows),
            pooled=sum(1 for r in rows if r.get("pooled")),
            suspects=suspects,
            filter=key,
        )
    )
