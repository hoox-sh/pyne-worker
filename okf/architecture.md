---
type: Reference
title: Architecture
description: The project shape and the OpenOrchestrate team.
tags: [architecture, openorchestrate]
status: stable
---

# Architecture

pyne-worker uses OpenOrchestrate. Languages: Python, Shell.

## Runtime surfaces

A Python Cloudflare Worker that evaluates Pine Script on the edge with the same
bar-loop contract as the PYNE Pro API. It is a thin wrap around a vendored
`pynescript` snapshot, not a replacement for the main PYNE repo.

- **Entry points:** the Worker's `POST /run` (evaluate, alerts, libraries), R2
  bar storage, a 1m cron, and L2 webhooks.
- **Run:** `bun run dev` locally, `bun run deploy` to Wrangler. `API_KEY` is
  required; R2 bucket `OHLCV_DATA` is provisioned from `wrangler.jsonc`.
- **Verify:** `./scripts/verify.sh` runs `ruff format --check`, `ruff check .`,
  and `npm run test`.

## The team

| Seat | Title | Agent |
| --- | --- | --- |
| Chief | Chief of Staff | `oo-chief` |
| Ops | Head of Operations | `oo-ops` |
| Checker | Quality Gate | `oo-checker` |
| Data | Head of Data | `oo-spec-1` |
| Engineering | Head of Engineering | `oo-spec-2` |
| Misc | General Desk | `oo-misc` |

## The board

The board lives at `.openorchestrate/board.md`. It has five statuses:
Todo, Working, Blocked, Ready for Review, Done. A card moves to Done only with proof.

## Related

- [Development](development.md)
- [Security](security.md)
