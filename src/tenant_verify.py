# Copyright (c) 2026 HOOX · PYNE · jango-blockchained
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Live tenant verification + usage metering for pyne-worker (stdlib only).

Console-issued ``hx_live_…`` keys are verified against the SaaS console::

    GET {CONSOLE_URL}/api/v1/verify?scope=pyne:run
    Authorization: Bearer <sha256hex(raw_key)>

Hash-on-wire: the raw key never leaves the isolate. The console looks the
key up by ``key_hash`` (the same column ``hoox-saas`` writes at key mint
time), so the full ``sha256hex`` on the wire is a lookup handle, not a
secret — and the raw key is never logged, cached, or transmitted.

Verified ``/run`` / ``/ingest`` calls queue one usage event each and flush
(up to 50 events per request) to::

    POST {CONSOLE_URL}/api/v1/usage

Both paths degrade safely: transport failures, unparseable bodies, and a
missing ``CONSOLE_URL`` all yield ``outcome="degraded"``. The request
pipeline (``handler.handle_request``) then fails open only for self-host
operators presenting the legacy key, and fails closed otherwise. Metering
failures are swallowed + logged and never fail the ``/run`` itself.

In-isolate cache: positive (``valid``/``denied``) 60 s, negative
(``invalid``/``degraded``/``limited``) 10 s, max 512 entries keyed by
``sha256hex(raw)[:16] + "|" + scope``. Scope-keyed so a verdict fetched
for one ``?scope=`` never poisons another scope.
"""

# pyne-worker — Python Cloudflare Worker for Pine Script evaluation
# Copyright (C) 2024-2026  jango-blockchained
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU Affero General Public License for more details.
#
# You should have received a copy of the GNU Affero General Public License
# along with this program.  If not, see <https://www.gnu.org/licenses/>.

from __future__ import annotations

import hashlib
import itertools
import json
import time
from dataclasses import dataclass
from dataclasses import field
from typing import Any
from urllib.parse import quote

# ---------------------------------------------------------------------------
# Contract constants
# ---------------------------------------------------------------------------

#: Scope required for ``POST /run`` and ``POST /ingest``.
TENANT_SCOPE = "pyne:run"

#: Billing upgrade URL surfaced on 402 ENTITLEMENT_REQUIRED.
UPGRADE_URL = "https://console.hoox.sh/billing"

#: Fallback console base URL (used only when callers pass an env object
#: without ``CONSOLE_URL``; ``handle_request`` passes ``None`` for
#: self-host passthrough instead — see :func:`verify_tenant`).
DEFAULT_CONSOLE_URL = "https://console.hoox.sh"

_VERIFY_PATH = "/api/v1/verify"
_USAGE_PATH = "/api/v1/usage"

#: Cache TTLs — positive (valid/denied) 60 s, negative (invalid/degraded) 10 s.
POSITIVE_TTL_SECONDS = 60.0
NEGATIVE_TTL_SECONDS = 10.0

#: Max verify cache entries (per isolate). Oldest non-fresh entries evicted.
VERIFY_CACHE_MAX = 512

#: Max usage events per flush POST; queue capped to bound isolate memory.
USAGE_BATCH_MAX = 50
USAGE_QUEUE_MAX = 500

_HTTP_TIMEOUT_SECONDS = 5.0
_USER_AGENT = "pyne-worker-tenant/0.6"


# ---------------------------------------------------------------------------
# Result type
# ---------------------------------------------------------------------------


@dataclass
class VerifyResult:
    """Outcome of a tenant key verification.

    ``outcome`` is one of:

    - ``"valid"`` — console says ``{ok: true}``; check :meth:`has_scope`.
    - ``"denied"`` — authenticated but not entitled (console HTTP 403).
    - ``"invalid"`` — unknown/revoked key (console ``{ok: false}`` / 401).
    - ``"limited"`` — console rate-limited the verify itself (HTTP 429);
      :attr:`retry_after` carries the ``Retry-After`` seconds when known.
    - ``"degraded"`` — console unreachable, unparseable, or unconfigured.
    """

    outcome: str
    tid: str | None = None
    plan: str | None = None
    scopes: list[str] = field(default_factory=list)
    limits: dict[str, Any] = field(default_factory=dict)
    retry_after: int | None = None

    def has_scope(self, scope: str) -> bool:
        """Return ``True`` when the verified key carries *scope*."""
        return scope in (self.scopes or [])

    def rate_limit(self) -> int | None:
        """Return the console-advertised calls/min limit, if any.

        Checks common limit key aliases; ``None`` means "no override —
        keep the local limiter default".
        """
        for key in (
            "calls_per_minute",
            "calls_per_min",
            "pyne_calls",
            "rate_limit",
            "limit",
        ):
            val = self.limits.get(key)
            if isinstance(val, bool):
                continue
            if isinstance(val, (int, float)) and val > 0:
                return int(val)
        return None


# ---------------------------------------------------------------------------
# Key hashing (hash-on-wire — raw keys never leave the isolate)
# ---------------------------------------------------------------------------


def key_hash_full(raw_key: str) -> str:
    """Return the full ``sha256hex`` of a tenant key.

    This is the wire token sent as ``Authorization: Bearer <hex>`` and the
    basis for cache keys / idempotency keys. It is a lookup handle, not a
    secret — but it is still never written to logs (log the 8-char prefix
    from ``middleware.tenant_key_hash_prefix`` instead).
    """
    return hashlib.sha256(raw_key.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# In-isolate verify cache
# ---------------------------------------------------------------------------

# key: sha256hex(raw)[:16] + "|" + scope → (expires_at_epoch, VerifyResult).
# Keyed by hash so raw keys never sit in isolate memory as map keys; the
# scope suffix stops a verdict fetched for one ?scope= from poisoning
# another (a key may be entitled for one scope but not another).
_VERIFY_CACHE: dict[str, tuple[float, VerifyResult]] = {}


def clear_verify_cache() -> None:
    """Drop all cached verify results (tests / key rotation)."""
    _VERIFY_CACHE.clear()


def _cache_store(cache_key: str, result: VerifyResult, expires_at: float) -> None:
    _VERIFY_CACHE[cache_key] = (expires_at, result)
    if len(_VERIFY_CACHE) <= VERIFY_CACHE_MAX:
        return
    now = time.time()
    for key, (exp, _) in list(_VERIFY_CACHE.items()):
        if exp <= now:
            _VERIFY_CACHE.pop(key, None)
            if len(_VERIFY_CACHE) <= VERIFY_CACHE_MAX:
                return
    while len(_VERIFY_CACHE) > VERIFY_CACHE_MAX:
        for key in _VERIFY_CACHE:
            if key != cache_key:
                _VERIFY_CACHE.pop(key, None)
                break
        else:
            break


# ---------------------------------------------------------------------------
# HTTP layer — Workers ``js.fetch`` when available, else urllib (stdlib).
# Module-level functions so tests can monkeypatch them (no network in CI).
# ---------------------------------------------------------------------------


def _parse_json_body(text: str | None) -> dict[str, Any] | None:
    if not text or not text.strip():
        return None
    try:
        data = json.loads(text)
    except ValueError:
        return None
    return data if isinstance(data, dict) else None


async def _fetch_json_get(
    url: str,
    headers: dict[str, str],
    timeout: float = _HTTP_TIMEOUT_SECONDS,
) -> tuple[int, dict[str, Any] | None, dict[str, str]]:
    """GET JSON. Returns ``(status, parsed_dict|None, response_headers)``.

    ``response_headers`` carries only the ``retry-after`` value (lowercased
    key) when the server sent one — enough for 429 ``Retry-After``
    propagation without exposing full header maps. Never raises.

    ``status == 0`` signals a transport failure (no HTTP response at all).
    """
    try:
        from js import Headers  # type: ignore[import-not-found]
        from js import fetch  # type: ignore[import-not-found]

        resp = await fetch(url, method="GET", headers=Headers.new(list(headers.items())))
        status = int(getattr(resp, "status", 0) or 0)
        try:
            retry_after: str | None = resp.headers.get("Retry-After")
        except Exception:
            retry_after = None
        try:
            text = await resp.text()
        except Exception:
            return status, None, _retry_headers(retry_after)
        return status, _parse_json_body(text), _retry_headers(retry_after)
    except ImportError:
        pass
    except Exception:
        return 0, None, {}

    import urllib.error
    import urllib.request

    req = urllib.request.Request(url, headers=dict(headers), method="GET")
    resp_headers: dict[str, str] = {}
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read()
            status = int(getattr(resp, "status", 200) or 200)
            try:
                resp_headers = _retry_headers(resp.headers.get("Retry-After"))
            except Exception:
                resp_headers = {}
    except urllib.error.HTTPError as e:
        try:
            raw = e.read() or b""
        except Exception:
            raw = b""
        status = int(e.code)
        try:
            resp_headers = _retry_headers(e.headers.get("Retry-After") if e.headers else None)
        except Exception:
            resp_headers = {}
    except Exception:
        return 0, None, {}
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        return status, None, resp_headers
    return status, _parse_json_body(text), resp_headers


def _retry_headers(value: Any) -> dict[str, str]:
    """Normalise a raw ``Retry-After`` header value to a headers dict."""
    if value is None:
        return {}
    text = str(value).strip()
    return {"retry-after": text} if text else {}


def _parse_retry_after(resp_headers: dict[str, Any] | None, body: dict[str, Any] | None) -> int | None:
    """Extract ``Retry-After`` seconds from response headers or body.

    Mirrors ``pyne-agent-worker`` ``verify.ts`` (numeric ``Retry-After``
    only — HTTP-dates are ignored): a finite integer ``>= 0``, else
    ``None``. Header wins; ``retry_after`` / ``retryAfter`` body fields
    are a fallback (handy for mocked transports that only fake status+body).
    """
    candidates: list[Any] = []
    if isinstance(resp_headers, dict):
        for key in ("retry-after", "Retry-After", "retry_after"):
            if resp_headers.get(key) is not None:
                candidates.append(resp_headers.get(key))
    if isinstance(body, dict):
        for key in ("retry_after", "retryAfter", "retry-after"):
            if body.get(key) is not None:
                candidates.append(body.get(key))
    for raw in candidates:
        try:
            parsed = int(str(raw).strip())
        except (TypeError, ValueError):
            continue
        if parsed >= 0:
            return parsed
    return None


async def _post_json(
    url: str,
    headers: dict[str, str],
    payload: dict[str, Any],
    timeout: float = _HTTP_TIMEOUT_SECONDS,
) -> int:
    """POST JSON. Returns HTTP status (``0`` on transport failure). Never raises."""
    data = json.dumps(payload).encode("utf-8")
    base = {
        "Content-Type": "application/json",
        "User-Agent": _USER_AGENT,
        **dict(headers),
    }
    try:
        from js import Headers  # type: ignore[import-not-found]
        from js import fetch  # type: ignore[import-not-found]

        resp = await fetch(
            url,
            method="POST",
            headers=Headers.new(list(base.items())),
            body=data.decode("utf-8"),
        )
        return int(getattr(resp, "status", 0) or 0)
    except ImportError:
        pass
    except Exception:
        return 0

    import urllib.error
    import urllib.request

    req = urllib.request.Request(url, data=data, method="POST", headers=base)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return int(getattr(resp, "status", 200) or 200)
    except urllib.error.HTTPError as e:
        return int(e.code)
    except Exception:
        return 0


# ---------------------------------------------------------------------------
# Verify
# ---------------------------------------------------------------------------


def _resolve_console_url(env: Any) -> str | None:
    """Extract the console base URL from an env binding / dict / string."""
    if env is None:
        return None
    if isinstance(env, str):
        return env.strip() or None
    raw: Any = env.get("CONSOLE_URL") if isinstance(env, dict) else getattr(env, "CONSOLE_URL", None)
    if isinstance(raw, str) and raw.strip():
        return raw.strip()
    return None


def _as_str_list(value: Any) -> list[str]:
    if isinstance(value, (list, tuple)):
        return [str(v) for v in value if isinstance(v, (str, int, float))]
    if isinstance(value, str):
        return [p.strip() for p in value.split(",") if p.strip()]
    return []


async def _verify_remote(console_url: str, raw_key: str, scope: str) -> VerifyResult:
    """Single uncached verify round-trip. Never raises (degraded on failure)."""
    key_hash = key_hash_full(raw_key)
    url = console_url.rstrip("/") + _VERIFY_PATH + "?scope=" + quote(scope, safe="")
    headers = {
        "Authorization": "Bearer " + key_hash,  # hash-on-wire — NEVER raw key
        "Accept": "application/json",
        "User-Agent": _USER_AGENT,
    }
    try:
        fetched = await _fetch_json_get(url, headers)
    except Exception:
        return VerifyResult(outcome="degraded")
    if isinstance(fetched, tuple) and len(fetched) == 3:
        status, body, resp_headers = fetched
    else:  # pragma: no cover — tolerates 2-tuple fakes in older tests
        status, body = fetched[0], fetched[1]
        resp_headers = {}
    if status == 0:
        return VerifyResult(outcome="degraded")
    if status == 403:
        # Authenticated but not entitled for this scope → 402 downstream.
        return VerifyResult(outcome="denied", scopes=[])
    if status == 401:
        return VerifyResult(outcome="invalid")
    if status == 429:
        # Console quota hit: fail closed with 429 downstream (mirrors
        # pyne-agent-worker verify.ts RATE_LIMITED + Retry-After). Never
        # misreported as 401 — the key is good, the quota is spent.
        return VerifyResult(
            outcome="limited",
            retry_after=_parse_retry_after(resp_headers, body),
        )
    if not isinstance(body, dict):
        # Unparseable success body: cannot decide — degrade, don't deny.
        return VerifyResult(outcome="degraded")
    if body.get("ok") is True:
        return VerifyResult(
            outcome="valid",
            tid=str(body["tid"]) if body.get("tid") is not None else None,
            plan=str(body["plan"]) if body.get("plan") is not None else None,
            scopes=_as_str_list(body.get("scopes")),
            limits=dict(body["limits"]) if isinstance(body.get("limits"), dict) else {},
        )
    # Console said {ok: false} — fail closed (429 is handled above).
    return VerifyResult(outcome="invalid")


async def verify_tenant(env: Any, raw_key: str, scope: str = TENANT_SCOPE) -> VerifyResult:
    """Verify a tenant key against the console with in-isolate caching.

    Args:
        env: Console URL source — env object with ``CONSOLE_URL``, a dict
            with ``"CONSOLE_URL"``, or the base URL string itself.
            Falsy/missing → ``outcome="degraded"`` (no network attempted).
        raw_key: The raw ``hx_live_…`` key (hashed before any use).
        scope: Required scope echoed as the ``?scope=`` param
            (default ``"pyne:run"``).

    Returns:
        Cached-or-fresh :class:`VerifyResult`. Never raises.
    """
    if not isinstance(raw_key, str) or not raw_key:
        return VerifyResult(outcome="invalid")
    norm_scope = scope or TENANT_SCOPE
    cache_key = f"{key_hash_full(raw_key)[:16]}|{norm_scope}"
    now = time.time()
    hit = _VERIFY_CACHE.get(cache_key)
    if hit is not None:
        expires_at, cached = hit
        if expires_at > now:
            return cached
        _VERIFY_CACHE.pop(cache_key, None)

    console_url = _resolve_console_url(env)
    if not console_url:
        result = VerifyResult(outcome="degraded")
    else:
        result = await _verify_remote(console_url, raw_key, norm_scope)

    ttl = POSITIVE_TTL_SECONDS if result.outcome in ("valid", "denied") else NEGATIVE_TTL_SECONDS
    _cache_store(cache_key, result, now + ttl)
    return result


# ---------------------------------------------------------------------------
# Usage metering — queue + background-style flush (lossy, never raises)
# ---------------------------------------------------------------------------

_USAGE_QUEUE: list[dict[str, Any]] = []
_USAGE_COUNTER = itertools.count(1)


def queue_usage_event(
    tid: str,
    scope: str,
    calls: int,
    bars: int,
    key_hash16: str,
) -> dict[str, Any]:
    """Queue one usage event. Returns the event (never raises).

    Idempotency key format: ``{UTC-day}:{hash16}:{counter}``, e.g.
    ``2026-10-09:9f2ac4110be3d4e5:42`` — the counter is isolate-monotonic
    so retries across requests never collide.
    """
    day = time.strftime("%Y-%m-%d", time.gmtime())
    seq = next(_USAGE_COUNTER)
    event = {
        "tid": tid,
        "scope": scope,
        "units": {"calls": int(calls), "bars": int(bars)},
        "idem": f"{day}:{key_hash16}:{seq}",
        "service": "pyne-worker",
        "day": day,
    }
    _USAGE_QUEUE.append(event)
    if len(_USAGE_QUEUE) > USAGE_QUEUE_MAX:
        del _USAGE_QUEUE[: len(_USAGE_QUEUE) - USAGE_QUEUE_MAX]
    return event


def clear_usage_queue() -> None:
    """Drop queued usage events (tests)."""
    _USAGE_QUEUE.clear()


def usage_queue_depth() -> int:
    """Return the number of queued (unflushed) usage events."""
    return len(_USAGE_QUEUE)


async def flush_usage_queue(console_url: str | None, auth_token: str | None = None) -> int:
    """POST up to :data:`USAGE_BATCH_MAX` queued events to the console.

    Args:
        console_url: Console base URL (falsy → no-op, queue retained).
        auth_token: ``Authorization: Bearer`` value — a service key when
            configured, else the per-request tenant key hash (hash-on-wire).

    Returns:
        Number of events accepted (2xx). The batch is dropped either way —
        metering is lossy by design so a console outage can never wedge the
        isolate or fail a ``/run``. Failures are printed (hash prefixes
        only, never raw keys). Never raises.
    """
    if not console_url or not _USAGE_QUEUE:
        return 0
    batch = _USAGE_QUEUE[:USAGE_BATCH_MAX]
    del _USAGE_QUEUE[: len(batch)]
    url = console_url.rstrip("/") + _USAGE_PATH
    headers: dict[str, str] = {"Accept": "application/json"}
    if auth_token:
        headers["Authorization"] = "Bearer " + auth_token
    try:
        status = await _post_json(url, headers, {"events": batch})
    except Exception as e:  # _post_json never raises — belt and braces
        print(json.dumps({"type": "usage_flush_error", "error": str(e)[:200], "dropped": len(batch)}))
        return 0
    if 200 <= status < 300:
        return len(batch)
    print(
        json.dumps(
            {
                "type": "usage_flush_rejected",
                "status": status,
                "dropped": len(batch),
                "first_idem": batch[0].get("idem") if batch else None,
            }
        )
    )
    return 0
