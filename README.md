# pyne-worker

Python Cloudflare Worker — Pine Script evaluation on the edge.

**Deployed at:** `https://pyne-worker.cryptolinx.workers.dev`

100% compatible with TradingView Pine Script v5/v6. Run your strategies
outside TradingView for **$0/month** on Cloudflare's free tier.

## Features

- ✅ **Full Pine Script v5/v6** — 500+ builtins via [pynescript](https://github.com/jango-blockchained/pynescript)
- ✅ **Authentication** — API key validation via `X-API-Key` header / `API_KEY` secret
- ✅ **Rate limiting** — sliding-window in-memory (100 req/60s)
- ✅ **Input validation** — script size (100KB), bar count (100K), payload size (5MB), field validation
- ✅ **Structured logging** — JSON per-request logs with request IDs and timing
- ✅ **Health checks** — `/health` verifies R2 + service binding connectivity
- ✅ **Execution timeout** — 30s wall-clock deadline per script run
- ✅ **R2 data ingestion** — `POST /ingest` for OHLCV (`1m` / `1h` / `1d` / …)
- ✅ **Compile modes** — `interpret` | `compile` | `auto` on `POST /run`
- ✅ **Deployed scripts** — R2 registry + `script_id` on `/run`
- ✅ **1m bar-close cron** — `* * * * *` + `POST /cron/run` (runs only on new bar)
- ✅ **Live market feed** — each cron tick pulls closed klines (Bybit → R2) before eval
- ✅ **Trade event forwarding** — strategy events → trade-worker via service binding
- ✅ **Alert engine** — `alert()` / `alertcondition()` firings in `/run` + cron (last-bar filter)
- ✅ **R2 data provider** — historical bar data from `data/{SYMBOL}/{TIMEFRAME}/{YYYY}.jsonl`
- ✅ **$0 infrastructure** — free tier OK for light use; Paid recommended for heavy 1m
- ✅ **3,263 BTCUSDT daily bars** preloaded in R2 (2017–2026); ingest `1m` for live cron

## Quick start

```bash
# Install
pip install -e ".[dev]"

# Run tests
pytest -v

# Sync pynescript into python_modules/ (CF vendored tree — required before deploy)
./scripts/sync_vendor.sh

# Deploy
npx wrangler deploy
# Then set your API key
echo "my-secret-key" | wrangler secret put API_KEY
```

## Endpoints

| Method | Path | Auth | Description |
|--------|------|------|-------------|
| GET | `/health` | No | Health check + feature flags |
| POST | `/run` | Yes | Execute Pine (`mode`, `script` or `script_id`) |
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
| `max_bars` | Tail length when loading long R2 history |

Also accepts `"data"` instead of `"ohlcv"` (pynescript Pro API compat).

### Deploy a script + 1m bar-close cron

```bash
# 1) Ingest 1m bars (from your machine — Binance blocks CF IPs)
python scripts/fetch_and_ingest.py \
  --symbol BTCUSDT --timeframe 1m \
  --ingest-url https://pyne-worker.cryptolinx.workers.dev/ingest \
  --api-key "$API_KEY"

# 2) Deploy Pine
curl -sS -X POST https://pyne-worker.cryptolinx.workers.dev/scripts \
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
curl -sS -X PUT https://pyne-worker.cryptolinx.workers.dev/cron/jobs \
  -H "Content-Type: application/json" -H "X-API-Key: $API_KEY" \
  -d '{"jobs":[{"script_id":"btc-sma","symbol":"BTCUSDT","timeframe":"1m","mode":"auto","enabled":true}]}'

# 4) Manual trigger (same path as Cron Trigger * * * * *)
curl -sS -X POST https://pyne-worker.cryptolinx.workers.dev/cron/run \
  -H "Content-Type: application/json" -H "X-API-Key: $API_KEY" \
  -d '{"force":true}'
```

Cron is configured in `wrangler.jsonc` as `* * * * *` (every minute UTC).

**Each cron tick:**

1. **Feed** — pull latest **closed** klines from **Bybit** (Binance fallback) for each job pair → R2  
2. **Eval** — run deployed scripts only if last closed bar time advanced  

No laptop feeder required for live 1m. Bulk history still optional via `fetch_and_ingest.py`.

```bash
# Manual feed only
curl -sS -X POST https://pyne-worker.cryptolinx.workers.dev/feed/refresh \
  -H "Content-Type: application/json" -H "X-API-Key: $API_KEY" \
  -d '{"symbol":"BTCUSDT","timeframe":"1m","limit":200}'
```

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

When `API_KEY` is unset (dev mode), auth is disabled.

## Data pipeline

### Live (default for cron)

Every minute the Worker itself refreshes closed candles via **Bybit** public API
into R2, then evaluates scripts. Binance is a fallback (often blocked on CF IPs).

### Bulk history (optional, off-edge)

Preload long history from Binance (runs locally / GH Actions):

```bash
# Fetch BTCUSDT 1m (or 1d / 1h) for current year
python scripts/fetch_and_ingest.py --symbol BTCUSDT --timeframe 1m
python scripts/fetch_and_ingest.py --symbol BTCUSDT --timeframe 1d

# Upload via the /ingest endpoint
python3 -c "
import gzip, json, urllib.request
with gzip.open('data/BTCUSDT/1d/2026.jsonl.gz', 'rt') as f:
    bars = [json.loads(line) for line in f if line.strip()]
body = json.dumps({'symbol': 'BTCUSDT', 'timeframe': '1d', 'bars': bars}).encode()
req = urllib.request.Request(
    'https://pyne-worker.cryptolinx.workers.dev/ingest',
    data=body,
    headers={'Content-Type': 'application/json', 'X-API-Key': 'your-key-here'},
    method='POST',
)
with urllib.request.urlopen(req) as resp:
    print(resp.read().decode())
"
```

A daily GitHub Actions workflow (`.github/workflows/data-ingest.yml`)
automatically fetches top symbols at 02:00 UTC.

**Note:** Binance blocks Cloudflare Workers IPs (HTTP 403), so auto-fetch
from the Worker itself is not supported. Preload data locally or provide
inline `"ohlcv"`/`"data"` in the request body.

## Architecture

1. **pynescript** — Parser, AST, evaluator + compile path (500+ builtins, Pine v5/v6)
2. **pynescript_backend** — Bar-loop runtime (`mode=interpret|compile|auto`)
3. **entry.py** — HTTP + `scheduled()` cron entrypoint, trade-worker forwarding
4. **handler.py** — Routing, middleware, `/run` / scripts / cron
5. **scripts_registry.py** — Deployed scripts + cron job config in R2
6. **scheduler.py** — Bar-close job runner (used by cron and `POST /cron/run`)
5. **data_provider.py** — R2 reader/writer (JSONL, `data/{SYM}/{TF}/{Y}.jsonl`)
6. **trade_forwarder.py** — StrategyEvent → trade-worker WebhookPayload mapping

## Parity

Python reference: [jango-blockchained/pynescript](https://github.com/jango-blockchained/pynescript)

TypeScript sibling: [pine-worker](https://github.com/jango-blockchained/pine-worker)

## Related

- [hoox.sh](https://hoox.sh) — Edge-native trading framework
- [pynescript](https://github.com/jango-blockchained/pynescript) — Pine Script evaluator
- [pine-worker](https://github.com/jango-blockchained/pine-worker) — TypeScript port
- [trade-worker](https://github.com/jango-blockchained/trade-worker) — Trade execution

## License

AGPL v3 or later. See [LICENSE](LICENSE).
