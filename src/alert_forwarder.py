# Copyright (c) 2026 HOOX · PYNE · jango-blockchained
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Alert webhook delivery for pyne-worker (roadmap L2).

Delivers last-bar ``alert()`` / ``alertcondition()`` firings to HTTP endpoints
configured via:

- Worker env secret/var ``ALERT_WEBHOOK_URL`` (default for all jobs)
- Per-job / per-script ``webhook_url`` (overrides default)
- Opt-out with ``forward_alerts: false`` on the job/script

Payloads are JSON objects suitable for generic receivers (Discord-compatible
``content`` is also set when only a message is present).
"""

from __future__ import annotations

import json
from typing import Any
from typing import Awaitable
from typing import Callable

HttpPostJson = Callable[[str, dict[str, Any]], Awaitable[int]]


def build_alert_payload(alert: dict[str, Any]) -> dict[str, Any]:
    """Build a JSON webhook body from a Runtime / cron alert dict."""
    message = str(alert.get("message") or "")
    title = alert.get("title")
    payload: dict[str, Any] = {
        "type": "pine_alert",
        "source": "pyne-worker",
        "message": message,
        "freq": str(alert.get("freq") or "once_per_bar"),
        "alert_source": str(alert.get("source") or "alert"),
    }
    if title:
        payload["title"] = str(title)
    for key in (
        "bar_index",
        "time",
        "symbol",
        "timeframe",
        "script_id",
        "deployed_script_id",
        "run_id",
    ):
        if alert.get(key) is not None:
            payload[key] = alert[key]
    # Discord / Slack-friendly fallbacks
    if title and message:
        payload["content"] = f"**{title}**: {message}"
    elif message:
        payload["content"] = message
    return payload


async def default_http_post_json(url: str, body: dict[str, Any]) -> int:
    """POST JSON — Workers ``js.fetch`` when available, else urllib."""
    data = json.dumps(body).encode("utf-8")
    try:
        from js import Headers  # type: ignore[import-not-found]
        from js import fetch  # type: ignore[import-not-found]

        headers = Headers.new(
            [
                ["Content-Type", "application/json"],
                ["User-Agent", "pyne-worker-alerts/0.5"],
                ["X-Source", "pyne-worker"],
            ]
        )
        resp = await fetch(
            url,
            method="POST",
            headers=headers,
            body=data.decode("utf-8"),
        )
        return int(getattr(resp, "status", 0) or 0)
    except ImportError:
        pass

    import urllib.error
    import urllib.request

    req = urllib.request.Request(
        url,
        data=data,
        method="POST",
        headers={
            "Content-Type": "application/json",
            "User-Agent": "pyne-worker-alerts/0.5",
            "X-Source": "pyne-worker",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            return int(getattr(resp, "status", 200) or 200)
    except urllib.error.HTTPError as e:
        return int(e.code)


async def forward_alerts(
    alerts: list[dict[str, Any]],
    webhook_url: str,
    *,
    http_post_json: HttpPostJson | None = None,
    batch: bool = True,
) -> dict[str, Any]:
    """POST alert firings to *webhook_url*.

    Args:
        alerts: Alert dicts (from Runtime / cron last-bar filter).
        webhook_url: Destination URL (https recommended).
        http_post_json: Injectable POST (tests); returns HTTP status.
        batch: If True (default), one POST with ``{"alerts": [...], "count": N}``.
            If False, one POST per alert.

    Returns:
        ``{forwarded, failed, errors, url}`` summary.
    """
    result: dict[str, Any] = {
        "forwarded": 0,
        "failed": 0,
        "errors": [],
        "url": webhook_url,
        "batch": batch,
    }
    if not webhook_url or not alerts:
        return result

    post = http_post_json or default_http_post_json
    payloads = [build_alert_payload(a) for a in alerts if isinstance(a, dict)]
    if not payloads:
        return result

    try:
        if batch:
            body = {
                "type": "pine_alert_batch",
                "source": "pyne-worker",
                "count": len(payloads),
                "alerts": payloads,
            }
            # Convenience single-message for Discord if only one alert
            if len(payloads) == 1 and payloads[0].get("content"):
                body["content"] = payloads[0]["content"]
            status = await post(webhook_url, body)
            if 200 <= int(status) < 300:
                result["forwarded"] = len(payloads)
            else:
                result["failed"] = len(payloads)
                result["errors"].append(f"batch HTTP {status}")
        else:
            for p in payloads:
                try:
                    status = await post(webhook_url, p)
                    if 200 <= int(status) < 300:
                        result["forwarded"] += 1
                    else:
                        result["failed"] += 1
                        result["errors"].append(
                            f"bar {p.get('bar_index', '?')}: HTTP {status}"
                        )
                except Exception as e:  # noqa: BLE001
                    result["failed"] += 1
                    result["errors"].append(f"bar {p.get('bar_index', '?')}: {e!s}")
    except Exception as e:  # noqa: BLE001
        result["failed"] = len(payloads)
        result["errors"].append(str(e))

    return result


def group_alerts_by_webhook(
    alerts: list[Any],
    default_url: str | None = None,
) -> dict[str, list[dict[str, Any]]]:
    """Bucket alerts by ``webhook_url`` field (or *default_url*).

    Skips entries with ``forward_alerts: false``.
    """
    by_url: dict[str, list[dict[str, Any]]] = {}
    for a in alerts or []:
        if not isinstance(a, dict):
            continue
        if a.get("forward_alerts") is False:
            continue
        url = a.get("webhook_url") or default_url
        if not url:
            continue
        url_s = str(url).strip()
        if not url_s:
            continue
        by_url.setdefault(url_s, []).append(a)
    return by_url


async def forward_alert_groups(
    alerts: list[Any],
    *,
    default_url: str | None = None,
    http_post_json: HttpPostJson | None = None,
    batch: bool = True,
) -> dict[str, Any]:
    """Forward all alerts, grouping by destination URL."""
    groups = group_alerts_by_webhook(alerts, default_url=default_url)
    out: dict[str, Any] = {
        "destinations": 0,
        "forwarded": 0,
        "failed": 0,
        "errors": [],
        "by_url": {},
    }
    for url, group in groups.items():
        meta = await forward_alerts(
            group,
            url,
            http_post_json=http_post_json,
            batch=batch,
        )
        out["destinations"] += 1
        out["forwarded"] += int(meta.get("forwarded") or 0)
        out["failed"] += int(meta.get("failed") or 0)
        out["errors"].extend(meta.get("errors") or [])
        out["by_url"][url] = meta
    return out
