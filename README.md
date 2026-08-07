# pyne-worker

> Production **Python Cloudflare® Worker** for the **[PYNE](https://hoox.sh/pyne)** stack —
> evaluate TradingView® Pine Script™ on the edge with the same bar-loop contract as the Pro API.

**Version:** 0.5.0 · **Runtime:** Cloudflare Workers (Python) · **Engine:** [`hoox-pyne`](https://pypi.org/project/hoox-pyne/) (`pynescript`)

**Website:** [hoox.sh/pyne](https://hoox.sh/pyne) · **Docs:** [hoox.sh/pyne/docs](https://hoox.sh/pyne/docs) · **Repo:** [hoox-sh/pyne-worker](https://github.com/hoox-sh/pyne-worker)

**Edge deploy (example):** [`https://pyne-worker.cryptolinx.workers.dev`](https://pyne-worker.cryptolinx.workers.dev)

_Pine Script™ and TradingView® are trademarks of TradingView, Inc. Cloudflare® is a trademark of Cloudflare, Inc. This project is an independent effort and is not affiliated with or endorsed by TradingView, Inc. or Cloudflare, Inc._

## Ecosystem

Part of the **[HOOX](https://hoox.sh)** open trading stack ([org: `hoox-sh`](https://github.com/hoox-sh)):

| Product | Role | Repo | Website |
|---------|------|------|---------|
| **HOOX** | Edge trading framework (Workers mesh) | [hoox-sh/hoox](https://github.com/hoox-sh/hoox) | [hoox.sh](https://hoox.sh) · [docs](https://docs.hoox.sh) |
| **PYNE** | Pine Script™ toolchain + Pro API | [hoox-sh/pyne](https://github.com/hoox-sh/pyne) | [hoox.sh/pyne](https://hoox.sh/pyne) · [docs](https://hoox.sh/pyne/docs) |
| **pyne-worker** | Python edge evaluate host (this repo) | [hoox-sh/pyne-worker](https://github.com/hoox-sh/pyne-worker) | [hoox.sh/pyne](https://hoox.sh/pyne) |
| **pyne-agent-worker** | NL → PYNE scripts (Workers AI™; optional validate via this worker) | [hoox-sh/pyne-agent-worker](https://github.com/hoox-sh/pyne-agent-worker) | [AXIS plugin docs](https://hoox.sh/axis/docs/plugins/pine-agent) · [PYNE agent](https://hoox.sh/pyne/docs/agent) |
| **pine-worker** | TypeScript edge evaluate + trade events | [hoox-sh/pine-worker](https://github.com/hoox-sh/pine-worker) | — |
| **AXIS** | Installable charting PWA | [hoox-sh/axis](https://github.com/hoox-sh/axis) | [hoox.sh/axis](https://hoox.sh/axis) · [docs](https://hoox.sh/axis/docs) |
| **trade-worker** | Multi-exchange order routing | [hoox-sh/trade-worker](https://github.com/hoox-sh/trade-worker) | — |

**Note:** [pyne-agent-worker](https://github.com/hoox-sh/pyne-agent-worker) is fully usable **without** this evaluate host (standalone chat). When `PYNE_WORKER_URL` or a service binding points here, the agent can **generate → `POST /run` → retry** for higher-quality scripts.

```text
AXIS / HOOX / CLI
        │  evaluate contract (POST /run)
        ▼
┌───────────────────┐     ┌────────────────────┐
│  pyne-worker      │────▶│  trade-worker      │  strategy events
│  (Python edge)    │     │  (execution)       │
└─────────┬─────────┘     └────────────────────┘
          │ engine
          ▼
   hoox-sh/pyne  (import: pynescript)
```

Local sibling layout (typical):

```text
~/Git/hoox            # edge stack (hoox-sh/hoox)
~/Git/pynescript      # PYNE core (GitHub: hoox-sh/pyne)
~/Git/pyne-worker     # this repo
~/Git/pine-worker     # TS edge sibling
~/Git/axis            # charting PWA
```

## Overview

**pyne-worker** is the production edge host for PYNE:

- Runs the full **pynescript** bar-loop (`mode=interpret|compile|auto`) on Cloudflare® Workers
- Speaks the shared **evaluate contract** with Flask Pro API, AXIS, and HOOX ([docs](https://hoox.sh/pyne/docs/api/contract))
- **Deployed scripts** + **1m bar-close cron** + live Bybit kline feed → R2
- **`alert()` / `alertcondition()`** export + **L2 HTTP webhooks**
- Strategy **trade events** forwarded to [trade-worker](https://github.com/hoox-sh/trade-worker) via service binding

Strong real-world Pine coverage via the open PYNE engine — **not** a claim of bit-identical TradingView® platform parity. See [PYNE docs](https://hoox.sh/pyne/docs) and the corpus status in [hoox-sh/pyne](https://github.com/hoox-sh/pyne).

## Features

- **Full Pine v5/v6 surface** — engine from [hoox-pyne](https://pypi.org/project/hoox-pyne/) / [hoox-sh/pyne](https://github.com/hoox-sh/pyne)
- **Auth** — `X-API-Key` / Worker secret `API_KEY`
- **Rate limiting** — sliding window (100 req / 60s)
- **Input validation** — script size (100KB), bars (100K), payload (5MB)
- **Structured logging** — JSON per-request with IDs + timing
- **Health** — `GET /health` (R2 + service binding checks)
- **30s wall timeout** per `/run`
- **R2 OHLCV** — `POST /ingest`, `data/{SYMBOL}/{TIMEFRAME}/{YYYY}.jsonl`
- **Compile modes** — `interpret` · `compile` · `auto` on `POST /run`
- **Script registry** — deploy by `script_id`, list/get/delete
- **1m bar-close cron** — `* * * * *` + `POST /cron/run` (runs only on new bar)
- **Live market feed** — closed klines (Bybit → R2, Binance fallback)
- **Trade forwarding** — strategy events → trade-worker
- **Alert engine + L2 webhooks** — `ALERT_WEBHOOK_URL` or per-job `webhook_url`
- **$0 tier OK** for light use; Paid Workers recommended for heavy 1m cron

Product docs: [Alerts](https://hoox.sh/pyne/docs/runtime/alerts) · [Evaluate contract](https://hoox.sh/pyne/docs/api/contract)

## Quick start

```bash
# Install (editable pynescript sibling via pyproject)
pip install -e ".[dev]"

# Tests
pytest -v

# Sync engine into python_modules/ (required before Cloudflare deploy)
./scripts/sync_vendor.sh

# Deploy
npx wrangler deploy
echo "my-secret-key" | wrangler secret put API_KEY
```

**Deploy gotcha:** Wrangler packages `python_modules/pynescript`, not the editable install.
After pulling new PYNE APIs, always re-run `./scripts/sync_vendor.sh`.

## Endpoints

| Method | Path | Auth | Description |
|--------|------|------|-------------|
| GET | `/health` | No | Health + feature flags |
| POST | `/run` | Yes | Evaluate Pine (`mode`, `script` or `script_id`) |
| POST | `/ingest` | Yes | Upload OHLCV to R2 (`1m`, `1h`, `1d`, …) |
| POST | `/scripts` | Yes | Deploy a Pine script (R2 registry) |
| GET | `/scripts` | Yes | List deployed scripts (no source) |
| GET | `/scripts/:id` | Yes | Get full deployed script |
| DELETE | `/scripts/:id` | Yes | Remove deployed script |
| GET | `/cron/jobs` | Yes | List bar-close cron jobs |
| PUT | `/cron/jobs` | Yes | Replace cron jobs config |
| POST | `/cron/run` | Yes | Manually trigger bar-close scheduler |
| POST | `/feed/refresh` | Yes | Pull latest klines into R2 (Bybit primary) |

### POST /run

```json
{
  "script": "//@version=5\nindicator('test')\nplot(close)",
  "ohlcv": [
    {"open": 100, "high": 105, "low": 95, "close": 102, "time": 1000, "volume": 1000},
    {"open": 102, "high": 108, "low": 101, "close": 106, "time": 2000, "volume": 1200}
  ],
  "symbol": "BTCUSDT",
  "mode": "auto"
}
```

| Field | Notes |
|-------|--------|
| `script` | Inline Pine source |
| `script_id` | Load deployed script from R2 (instead of `script`) |
| `ohlcv` / `data` | Bars; or omit and set `symbol` + `timeframe` to read R2 |
| `mode` | `interpret` (default) · `compile` · `auto` (compile then fall back) |
| `inputs` | Optional `input.*` overrides (title → value); forces interpret under `auto` |
| `max_bars` | Tail length when loading long R2 history |

Also accepts `"data"` instead of `"ohlcv"` (PYNE Pro API compat). Response includes `plots`, `series`, `alerts`, `events`, `inputs`, `meta`, and structured `error` / `error_kind` on failure.

### Deploy a script + 1m bar-close cron

```bash
export WORKER=https://pyne-worker.cryptolinx.workers.dev   # or your deploy URL

# 1) Ingest 1m bars (from your machine — Binance blocks many CF IPs)
python scripts/fetch_and_ingest.py \
  --symbol BTCUSDT --timeframe 1m \
  --ingest-url "$WORKER/ingest" \
  --api-key "$API_KEY"

# 2) Deploy Pine
curl -sS -X POST "$WORKER/scripts" \
  -H "Content-Type: application/json" -H "X-API-Key: $API_KEY" \
  -d '{
    "id": "btc-sma",
    "script": "//@version=5\nstrategy(\"s\")\nplot(close)",
    "symbol": "BTCUSDT",
    "timeframe": "1m",
    "mode": "auto",
    "enabled": true,
    "max_bars": 5000
  }'

# 3) Optional: explicit cron job list (else all enabled scripts are scheduled)
curl -sS -X PUT "$WORKER/cron/jobs" \
  -H "Content-Type: application/json" -H "X-API-Key: $API_KEY" \
  -d '{"jobs":[{"script_id":"btc-sma","symbol":"BTCUSDT","timeframe":"1m","mode":"auto","enabled":true}]}'

# 4) Manual trigger (same path as Cron Trigger * * * * *)
curl -sS -X POST "$WORKER/cron/run" \
  -H "Content-Type: application/json" -H "X-API-Key: $API_KEY" \
  -d '{"force":true}'
```

Cron is configured in `wrangler.jsonc` as `* * * * *` (every minute UTC).

**Each cron tick:**

1. **Feed** — pull latest **closed** klines from **Bybit** (Binance fallback) for each job pair → R2  
2. **Eval** — run deployed scripts only if last closed bar time advanced  

No laptop feeder required for live 1m. Bulk history still optional via `fetch_and_ingest.py`.

### Alert webhooks (L2)

On each cron tick (and on `POST /run` / `POST /cron/run` when alerts are present),
firings are POSTed as JSON to:

1. Per-job / per-script `webhook_url` (highest priority)
2. Else Worker env `ALERT_WEBHOOK_URL`

```bash
export WORKER=https://pyne-worker.cryptolinx.workers.dev

# Default destination for all jobs
echo "https://hooks.example.com/pine" | wrangler secret put ALERT_WEBHOOK_URL

# Or per script when deploying
curl -sS -X POST "$WORKER/scripts" \
  -H "Content-Type: application/json" -H "X-API-Key: $API_KEY" \
  -d '{
    "id":"btc-alerts",
    "script":"//@version=5\nindicator(\"a\")\nalertcondition(ta.crossover(close, ta.sma(close,20)), \"X\", \"cross\")\nplot(close)",
    "symbol":"BTCUSDT","timeframe":"1m",
    "webhook_url":"https://hooks.example.com/btc",
    "forward_alerts":true
  }'
```

Batch body shape:

```json
{
  "type": "pine_alert_batch",
  "source": "pyne-worker",
  "count": 1,
  "content": "cross",
  "alerts": [
    {
      "type": "pine_alert",
      "message": "cross",
      "title": "X",
      "freq": "once_per_bar",
      "alert_source": "alertcondition",
      "symbol": "BTCUSDT",
      "timeframe": "1m",
      "bar_index": 42,
      "time": 1700000000000
    }
  ]
}
```

Opt out: `"forward_alerts": false` on the job/script or request body.

```bash
# Manual feed only
curl -sS -X POST "$WORKER/feed/refresh" \
  -H "Content-Type: application/json" -H "X-API-Key: $API_KEY" \
  -d '{"symbol":"BTCUSDT","timeframe":"1m","limit":200}'
```

Docs: [PYNE alerts + L2 webhooks](https://hoox.sh/pyne/docs/runtime/alerts)

### POST /ingest

```json
{
  "symbol": "BTCUSDT",
  "timeframe": "1m",
  "bars": [
    {"open": 100, "high": 105, "low": 95, "close": 102, "time": 1000, "volume": 1000}
  ]
}
```

## Auth

Set your API key as a Worker secret:

```bash
echo "your-secret-here" | wrangler secret put API_KEY
```

All authenticated routes (`/run`, `/ingest`, `/scripts`, `/cron/*`, `/feed/*`) must include:

```
X-API-Key: your-secret-here
```

When `API_KEY` is unset (dev mode), auth is disabled. **Production must set `API_KEY`** — without it every management route is open.

Auth comparison is constant-time (`hmac.compare_digest`). Rate limit: 100 req / 60s per key (in-memory per isolate).

### Webhook / SSRF notes

`webhook_url` (per-script / per-job / request body) and `ALERT_WEBHOOK_URL` are validated before delivery:

- **HTTPS only** (public hosts)
- Blocks `localhost`, `*.local` / `*.internal`, private / loopback / link-local / metadata IPs
- Blocks embedded credentials (`https://user:pass@…`)

R2 keys for OHLCV use sanitized `symbol` + `timeframe` only (`data/{SYMBOL}/{TF}/{YYYY}.jsonl`) to prevent path traversal.

## Data pipeline

### Live (default for cron)

Every minute the Worker itself refreshes closed candles via **Bybit** public API
into R2, then evaluates scripts. Binance is a fallback (often blocked on CF IPs).

### Bulk history (optional, off-edge)

Preload long history from Binance (runs locally / GH Actions):

```bash
export WORKER=https://pyne-worker.cryptolinx.workers.dev

# Fetch BTCUSDT 1m (or 1d / 1h) for current year
python scripts/fetch_and_ingest.py --symbol BTCUSDT --timeframe 1m
python scripts/fetch_and_ingest.py --symbol BTCUSDT --timeframe 1d

# Upload via the /ingest endpoint
python3 -c "
import gzip, json, urllib.request, os
with gzip.open('data/BTCUSDT/1d/2026.jsonl.gz', 'rt') as f:
    bars = [json.loads(line) for line in f if line.strip()]
body = json.dumps({'symbol': 'BTCUSDT', 'timeframe': '1d', 'bars': bars}).encode()
req = urllib.request.Request(
    os.environ['WORKER'] + '/ingest',
    data=body,
    headers={'Content-Type': 'application/json', 'X-API-Key': os.environ['API_KEY']},
    method='POST',
)
with urllib.request.urlopen(req) as resp:
    print(resp.read().decode())
"
```

A daily GitHub Actions workflow (`.github/workflows/data-ingest.yml`)
can fetch top symbols on a schedule.

**Note:** Binance blocks many Cloudflare Workers IPs (HTTP 403), so auto-fetch
from the Worker itself is limited. Live cron uses Bybit first; bulk preload
locally or pass inline `"ohlcv"` / `"data"`.

## Architecture

1. **[hoox-sh/pyne](https://github.com/hoox-sh/pyne)** (`pynescript`) — Parser, AST, evaluator + compile path  
2. **`pynescript_backend`** — Bar-loop Runtime port of `pyne/backend` (`interpret|compile|auto`)  
3. **`entry.py`** — HTTP + `scheduled()` cron entrypoint, trade + alert forwarding  
4. **`handler.py`** — Routing, middleware, `/run` / scripts / cron  
5. **`scripts_registry.py`** — Deployed scripts + cron job config in R2  
6. **`scheduler.py`** — Bar-close job runner  
7. **`data_provider.py`** — R2 reader/writer (`data/{SYM}/{TF}/{Y}.jsonl`)  
8. **`trade_forwarder.py`** — StrategyEvent → trade-worker WebhookPayload  
9. **`alert_engine.py` / `alert_forwarder.py`** — last-bar filter + HTTP webhooks  

Vendored deploy tree: `python_modules/` (sync with `./scripts/sync_vendor.sh`).

## Related

| Link | What |
|------|------|
| [hoox.sh](https://hoox.sh) | HOOX product home |
| [hoox.sh/pyne](https://hoox.sh/pyne) | PYNE product + playground entry |
| [hoox.sh/pyne/docs](https://hoox.sh/pyne/docs) | PYNE manuals (runtime, API, alerts) |
| [hoox-sh/pyne](https://github.com/hoox-sh/pyne) | Pine engine (this worker’s dependency) |
| [hoox-sh/pine-worker](https://github.com/hoox-sh/pine-worker) | TypeScript edge sibling |
| [hoox-sh/trade-worker](https://github.com/hoox-sh/trade-worker) | Exchange execution |
| [hoox-sh/hoox](https://github.com/hoox-sh/hoox) | Edge trading framework |
| [hoox-sh/axis](https://github.com/hoox-sh/axis) | Charting PWA |
| [PyPI: hoox-pyne](https://pypi.org/project/hoox-pyne/) | Installable engine |

## License

AGPL-3.0-or-later. See [LICENSE](LICENSE) and [NOTICE](NOTICE).
