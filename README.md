# pyne-worker

Experimental **Python** Cloudflare Worker that runs TradingView Pine Script
strategy scripts via [pynescript](https://github.com/jango-blockchained/pynescript)
and emits structured trade events to `trade-worker`.

This is the Python-native sibling of [pine-worker](../pine-worker) (TypeScript
port). Both workers share the same parity contract with pynescript's
`StrategyEvent` shape.

## Quick start

```bash
# From hoox-setup root — pynescript is resolved via ../../../pynescript
cd workers/pyne-worker
uv sync
uv run pytest

# Local dev (requires workers-py / pywrangler)
uv run pywrangler dev

# Deploy
uv run pywrangler deploy
```

## Endpoints

| Method | Path     | Description                          |
|--------|----------|--------------------------------------|
| GET    | `/health`| Health check                         |
| POST   | `/run`   | Execute a Pine Script strategy       |

### POST /run

```json
{
  "script": "//@version=5\nstrategy('test')\n...",
  "ohlcv": [
    { "open": 100, "high": 105, "low": 95, "close": 102, "time": 1000 }
  ],
  "symbol": "BTCUSDT"
}
```

Also accepts `"data"` instead of `"ohlcv"` (pynescript Pro API compat).

## Architecture

1. **pynescript** — Parser, AST, evaluator builtins (installed dependency)
2. **pynescript_backend** — Bar-loop runtime vendored from `pynescript/backend/`
3. **Service binding** — `TRADE_SERVICE` → `trade-worker` (forwarding TBD)
4. **R2** — `OHLCV_DATA` bucket for historical data (data layer TBD)

## Parity oracle

The reference Python implementation lives in
[jango-blockchained/pynescript](https://github.com/jango-blockchained/pynescript).
The parity corpus under `pynescript/tests/fixtures/parity/` defines the
contract shared with pine-worker.

## Related

- [pynescript](https://github.com/jango-blockchained/pynescript) — Python reference evaluator
- [pine-worker](https://github.com/jango-blockchained/pine-worker) — TypeScript port
- [trade-worker](https://github.com/jango-blockchained/trade-worker) — Trade execution worker
- [hoox-setup](https://github.com/jango-blockchained/hoox-setup) — Monorepo root