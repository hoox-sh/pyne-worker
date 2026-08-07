# Copyright (c) 2026 HOOX · PYNE · jango-blockchained
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Deployed Pine script registry stored in R2.

Keys:
  ``scripts/{id}.json``     — full script record (source + run defaults)
  ``scripts/_index.json``   — ``{"ids": ["a", "b", ...]}``
  ``config/cron_jobs.json`` — scheduled jobs list
  ``state/cron/{id}.json``  — last processed bar time per job
"""

from __future__ import annotations

import json
import re
import time
from typing import Any

from data_provider import _is_r2_hit
from security import sanitize_symbol
from security import sanitize_timeframe
from security import validate_webhook_url

_SCRIPT_ID_RE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9._-]{0,63}$")
_MAX_SCRIPT_LENGTH = 100_000
_VALID_MODES = frozenset({"interpret", "compile", "auto"})


def _script_key(script_id: str) -> str:
    return f"scripts/{script_id}.json"


def _index_key() -> str:
    return "scripts/_index.json"


def _cron_jobs_key() -> str:
    return "config/cron_jobs.json"


def _cron_state_key(job_id: str) -> str:
    return f"state/cron/{job_id}.json"


def validate_script_id(script_id: str) -> str | None:
    """Return error message if invalid, else None."""
    if not script_id or not isinstance(script_id, str):
        return "Missing or invalid 'id'"
    if not _SCRIPT_ID_RE.match(script_id):
        return (
            "Invalid script id: use 1–64 chars of [A-Za-z0-9._-], "
            "starting with alphanumeric"
        )
    if script_id.startswith("_"):
        return "Script id must not start with '_'"
    return None


async def _get_json(bucket: Any, key: str) -> dict[str, Any] | None:
    obj = await bucket.get(key)
    if not _is_r2_hit(obj):
        return None
    raw = await obj.text()
    if raw is None:
        return None
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return None
    return data if isinstance(data, dict) else None


async def _put_json(bucket: Any, key: str, data: dict[str, Any]) -> None:
    await bucket.put(key, json.dumps(data, separators=(",", ":")))


async def load_index(bucket: Any) -> list[str]:
    data = await _get_json(bucket, _index_key())
    if not data:
        return []
    ids = data.get("ids")
    if not isinstance(ids, list):
        return []
    return [str(i) for i in ids if isinstance(i, str)]


async def _save_index(bucket: Any, ids: list[str]) -> None:
    # stable unique order
    seen: set[str] = set()
    ordered: list[str] = []
    for i in ids:
        if i not in seen:
            seen.add(i)
            ordered.append(i)
    await _put_json(bucket, _index_key(), {"ids": ordered})


async def put_script(bucket: Any, record: dict[str, Any]) -> dict[str, Any]:
    """Create or update a deployed script. Returns the stored record."""
    script_id = record.get("id")
    err = validate_script_id(str(script_id) if script_id is not None else "")
    if err:
        raise ValueError(err)

    script = record.get("script")
    if not script or not isinstance(script, str):
        raise ValueError("Missing or invalid 'script'")
    if len(script) > _MAX_SCRIPT_LENGTH:
        raise ValueError(f"Script exceeds {_MAX_SCRIPT_LENGTH} character limit")

    mode = str(record.get("mode") or "auto").strip().lower()
    if mode not in _VALID_MODES:
        raise ValueError(f"Invalid mode {mode!r}; use interpret|compile|auto")

    symbol = sanitize_symbol(str(record.get("symbol") or "BTCUSDT"))
    if symbol is None:
        raise ValueError(
            "Invalid symbol: use 1–32 chars of [A-Za-z0-9._:-], no path separators"
        )
    timeframe = sanitize_timeframe(str(record.get("timeframe") or "1m"))
    if timeframe is None:
        raise ValueError(
            "Invalid timeframe; allowed: 1m,3m,5m,15m,30m,1h,2h,4h,6h,8h,12h,1d,3d,1w,1M"
        )

    max_bars = record.get("max_bars", 5000)
    try:
        max_bars_i = int(max_bars)
    except (TypeError, ValueError) as e:
        raise ValueError("'max_bars' must be an integer") from e
    if max_bars_i < 50 or max_bars_i > 100_000:
        raise ValueError("'max_bars' must be between 50 and 100000")

    stored = {
        "id": script_id,
        "name": str(record.get("name") or script_id),
        "script": script,
        "symbol": symbol,
        "timeframe": timeframe,
        "mode": mode,
        "enabled": bool(record.get("enabled", True)),
        "forward_events": bool(record.get("forward_events", True)),
        "forward_alerts": bool(record.get("forward_alerts", True)),
        "max_bars": max_bars_i,
        "updated_at": int(time.time() * 1000),
    }
    # Optional HTTPS destination for pine alert() / alertcondition() firings
    wh = record.get("webhook_url")
    if isinstance(wh, str) and wh.strip():
        safe_wh = validate_webhook_url(wh)
        if safe_wh is None:
            raise ValueError(
                "Invalid webhook_url: must be https to a public host "
                "(no localhost / private IP / credentials)"
            )
        stored["webhook_url"] = safe_wh

    await _put_json(bucket, _script_key(str(script_id)), stored)
    ids = await load_index(bucket)
    if script_id not in ids:
        ids.append(str(script_id))
        await _save_index(bucket, ids)
    return stored


async def get_script(bucket: Any, script_id: str) -> dict[str, Any] | None:
    err = validate_script_id(script_id)
    if err:
        return None
    return await _get_json(bucket, _script_key(script_id))


async def delete_script(bucket: Any, script_id: str) -> bool:
    err = validate_script_id(script_id)
    if err:
        return False
    existing = await get_script(bucket, script_id)
    if existing is None:
        return False
    # R2 delete may not exist on all mocks — put empty index entry removal
    try:
        await bucket.delete(_script_key(script_id))
    except Exception:
        # Best-effort: overwrite with disabled stub if delete unsupported
        await _put_json(
            bucket,
            _script_key(script_id),
            {**existing, "enabled": False, "script": "", "deleted": True},
        )
    ids = await load_index(bucket)
    ids = [i for i in ids if i != script_id]
    await _save_index(bucket, ids)
    return True


async def list_scripts(bucket: Any) -> list[dict[str, Any]]:
    ids = await load_index(bucket)
    out: list[dict[str, Any]] = []
    for sid in ids:
        rec = await get_script(bucket, sid)
        if rec and not rec.get("deleted"):
            # omit full source in list view
            item: dict[str, Any] = {
                "id": rec.get("id"),
                "name": rec.get("name"),
                "symbol": rec.get("symbol"),
                "timeframe": rec.get("timeframe"),
                "mode": rec.get("mode"),
                "enabled": rec.get("enabled", True),
                "forward_events": rec.get("forward_events", True),
                "forward_alerts": rec.get("forward_alerts", True),
                "max_bars": rec.get("max_bars"),
                "updated_at": rec.get("updated_at"),
            }
            if rec.get("webhook_url"):
                item["webhook_url"] = rec.get("webhook_url")
            out.append(item)
    return out


async def load_cron_jobs(bucket: Any) -> list[dict[str, Any]]:
    """Load cron job configs.

    Prefer ``config/cron_jobs.json``. If missing, derive jobs from enabled
    scripts in the registry (one job per enabled script).
    """
    data = await _get_json(bucket, _cron_jobs_key())
    if data and isinstance(data.get("jobs"), list):
        jobs: list[dict[str, Any]] = []
        for j in data["jobs"]:
            if isinstance(j, dict) and j.get("script_id"):
                jobs.append(j)
        return jobs

    # Auto-derive from deployed scripts
    derived: list[dict[str, Any]] = []
    for rec in await list_scripts(bucket):
        if not rec.get("enabled", True):
            continue
        job: dict[str, Any] = {
            "script_id": rec["id"],
            "symbol": rec.get("symbol") or "BTCUSDT",
            "timeframe": rec.get("timeframe") or "1m",
            "mode": rec.get("mode") or "auto",
            "enabled": True,
            "max_bars": rec.get("max_bars") or 5000,
            "forward_events": rec.get("forward_events", True),
            "forward_alerts": rec.get("forward_alerts", True),
        }
        if rec.get("webhook_url"):
            job["webhook_url"] = rec.get("webhook_url")
        derived.append(job)
    return derived


async def put_cron_jobs(bucket: Any, jobs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    cleaned: list[dict[str, Any]] = []
    for j in jobs:
        if not isinstance(j, dict):
            continue
        sid = j.get("script_id")
        if not sid or not isinstance(sid, str):
            continue
        err = validate_script_id(sid)
        if err:
            continue
        tf = sanitize_timeframe(str(j.get("timeframe") or "1m")) or "1m"
        mode = str(j.get("mode") or "auto").strip().lower()
        if mode not in _VALID_MODES:
            mode = "auto"
        sym = sanitize_symbol(str(j.get("symbol") or "BTCUSDT")) or "BTCUSDT"
        try:
            max_bars_i = int(j.get("max_bars") or 5000)
        except (TypeError, ValueError):
            max_bars_i = 5000
        max_bars_i = max(50, min(max_bars_i, 100_000))
        entry: dict[str, Any] = {
            "script_id": sid,
            "symbol": sym,
            "timeframe": tf,
            "mode": mode,
            "enabled": bool(j.get("enabled", True)),
            "max_bars": max_bars_i,
            "forward_events": bool(j.get("forward_events", True)),
            "forward_alerts": bool(j.get("forward_alerts", True)),
        }
        wh = j.get("webhook_url")
        if isinstance(wh, str) and wh.strip():
            safe_wh = validate_webhook_url(wh)
            if safe_wh is not None:
                entry["webhook_url"] = safe_wh
            # Invalid webhook silently dropped (fail-closed for SSRF)
        cleaned.append(entry)
    await _put_json(bucket, _cron_jobs_key(), {"jobs": cleaned})
    return cleaned


async def get_cron_state(bucket: Any, job_id: str) -> dict[str, Any]:
    data = await _get_json(bucket, _cron_state_key(job_id))
    return data or {}


async def put_cron_state(bucket: Any, job_id: str, state: dict[str, Any]) -> None:
    await _put_json(bucket, _cron_state_key(job_id), state)
