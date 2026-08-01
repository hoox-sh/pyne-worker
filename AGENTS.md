# pyne-worker — Agent Instructions

Python Cloudflare Worker that evaluates Pine Script strategies on the edge using the `pynescript` package.

## Stack

| Layer | Choice | Notes |
|---|---|---|
| Runtime | Python (Cloudflare Workers Python) | `workers-py` SDK |
| Pine Script engine | `pynescript` (editable install) | Python parser/evaluator from sibling repo |
| Package mgr | uv / pip | `pyproject.toml` with Hatch-style `[project]` config |
| Deploy | wrangler | Cloudflare Workers Python runtime |

## Architecture

`pyne-worker` depends on `pynescript` (sibling repo at `/home/jango/Git/pynescript`, editable install). It runs Pine Script evaluation, R2 data ingestion, and trade event forwarding on Cloudflare Workers using the Python Workers runtime.

## Sister Repos

All repos below are part of the **HOOX** project. When working on pyne-worker, be aware of the others.

| Repo | Path | Purpose |
|---|---|---|
| `hoox-setup` | `/home/jango/Git/hoox-setup` | Monorepo: all Cloudflare Workers, Docker, CI/CD |
| `hoox-landing-page` | `/home/jango/Git/hoox-landing-page` | Marketing landing site (Next.js 16) |
| `pynescript` | `/home/jango/Git/pynescript` | Pine Script parser/evaluator (Python, ANTLR4) — **this repo's core dependency** |
| `pyne-worker` | `/home/jango/Git/pyne-worker` | Python Cloudflare Worker — Pine Script evaluation on the edge |
| `pine-worker` | `/home/jango/Git/pine-worker` | TypeScript Cloudflare Worker — Pine Script evaluator + trade event emitter |

**Key:** `pyne-worker` → `pynescript` (editable install). `pine-worker` depends on `@jango-blockchained/hoox-shared` from `hoox-setup`.
```
pynescript ←── pyne-worker (depends on this)
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
| `pyproject.toml` | Package config (editable dep on `pynescript`) |

### Alerts (L2)

- Runtime exports `alerts` on `/run` (pynescript `AlertsMixin`).
- Cron keeps last closed-bar firings only; POSTs to job `webhook_url` or env `ALERT_WEBHOOK_URL`.
- Product docs (Mintlify): `pynescript` → `docs/pyne/runtime/alerts.mdx`.
