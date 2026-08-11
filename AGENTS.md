# pyne-worker — Agent Instructions

Python Cloudflare Worker that evaluates Pine Script strategies on the edge using the `pynescript` package.

## Stack

| Layer | Choice | Notes |
|---|---|---|
| Runtime | Python (Cloudflare Workers Python) | `workers-py` SDK |
| Pine Script engine | `pynescript` (editable install) | Package SoT bar-loop (`pynescript.runtime`) |
| Package mgr | uv / pip | `pyproject.toml` with Hatch-style `[project]` config |
| Deploy | wrangler | Cloudflare Workers Python runtime |

## Architecture

`pyne-worker` depends on `pynescript` (sibling repo at `/home/jango/Git/pynescript`, editable install). **H1:** the bar-loop is the package Runtime (`pynescript.runtime`); `src/pynescript_backend/` is a thin edge wrap (strict OHLCV validation + re-exports). Deploy still vendors the package tree via `./scripts/sync_vendor.sh` into `python_modules/`.

## Sister Repos

All repos below are part of the **HOOX** stack under GitHub org **[`hoox-sh`](https://github.com/hoox-sh)**.
Product site: [hoox.sh](https://hoox.sh) · PYNE: [hoox.sh/pyne](https://hoox.sh/pyne).

| Repo (GitHub) | Local path | Purpose |
|---|---|---|
| [hoox-sh/hoox](https://github.com/hoox-sh/hoox) | `/home/jango/Git/hoox` (or `hoox-setup`) | Edge trading framework / Workers mesh |
| [hoox-sh/pyne](https://github.com/hoox-sh/pyne) | `/home/jango/Git/pynescript` | Pine Script™ engine — **this repo's core dependency** |
| [hoox-sh/pyne-worker](https://github.com/hoox-sh/pyne-worker) | `/home/jango/Git/pyne-worker` | Python CF Worker — edge evaluate (this repo) |
| [hoox-sh/pine-worker](https://github.com/hoox-sh/pine-worker) | `/home/jango/Git/pine-worker` | TypeScript CF Worker — evaluate + trade events |
| [hoox-sh/trade-worker](https://github.com/hoox-sh/trade-worker) | — | Multi-exchange execution |
| [hoox-sh/axis](https://github.com/hoox-sh/axis) | `/home/jango/Git/axis` | Charting PWA |
| landing | `/home/jango/Git/hoox-landing-page` | Marketing site (Next.js → hoox.sh) |

**Key:** `pyne-worker` → `pynescript` (editable install of [hoox-sh/pyne](https://github.com/hoox-sh/pyne)).
```
hoox-sh/pyne (pynescript) ←── pyne-worker (depends on this)
         │
         └── shared Pine Script parser/evaluator
```

## Commands

```bash
pip install -e ".[dev]"    # install + dev deps
pytest tests/ -v            # run tests
./scripts/sync_vendor.sh   # copy live pynescript → python_modules/ (required before deploy)
npx wrangler deploy        # deploy (uses python_modules/ as Vendored Modules)
```

**Deploy gotcha:** Cloudflare packages `python_modules/pynescript`, not the editable
install. After pulling new `pynescript` APIs (e.g. `util/time_parts.py`), always run
`./scripts/sync_vendor.sh` or deploy will fail with `ModuleNotFoundError`.

## Key Files

| File | Purpose |
|---|---|
| `src/entry.py` | Worker entry (`fetch` + `scheduled` cron) |
| `src/handler.py` | HTTP routes: `/run`, `/scripts`, `/cron/*`, `/ingest` |
| `src/scheduler.py` | Bar-close job runner |
| `src/scripts_registry.py` | Deployed scripts + cron config in R2 |
| `src/alert_engine.py` | Last-bar alert filter + summary helpers |
| `src/alert_forwarder.py` | L2 HTTP webhook delivery for alerts |
| `wrangler.jsonc` | Worker config + cron `* * * * *` |
| `src/pynescript_backend/` | Thin wrap over `pynescript.runtime` (strict bars) |
| `pyproject.toml` | Package config (editable dep on `pynescript`) |

### Alerts (L2)

- Runtime exports `alerts` on `/run` (pynescript `AlertsMixin`).
- Cron keeps last closed-bar firings only; POSTs to job `webhook_url` or env `ALERT_WEBHOOK_URL`.
- Webhook URLs must be **HTTPS public hosts** (`security.validate_webhook_url`) — private IPs / localhost rejected.
- Product docs: [hoox.sh/pyne/docs/runtime/alerts](https://hoox.sh/pyne/docs/runtime/alerts)
  (source: [hoox-sh/pyne](https://github.com/hoox-sh/pyne) → `docs/pyne/runtime/alerts.mdx`).

### Security helpers

| Module | Role |
|---|---|
| `src/security.py` | Symbol/timeframe sanitization (R2 path safety), webhook SSRF checks |
| `src/middleware.py` | `validate_api_key` (constant-time), rate limiter |
| Auth | `API_KEY` secret + `X-API-Key` — **not** mesh `INTERNAL_KEY_BINDING` |
