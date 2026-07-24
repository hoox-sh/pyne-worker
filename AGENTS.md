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
uv run wrangler dev         # local dev
uv run wrangler deploy      # deploy
```

## Key Files

| File | Purpose |
|---|---|
| `src/entry.py` | Worker entry point |
| `wrangler.jsonc` | Worker config |
| `pyproject.toml` | Package config (editable dep on `pynescript`) |
