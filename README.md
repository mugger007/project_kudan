# Project Kudan

Async Python 3.12 trading bot for [Polymarket](https://polymarket.com/) prediction markets. Focuses on high-probability entries on Elon Musk tweet-count events and Bitcoin price events. Optimised for free-tier always-on deployment on [ClawCloud Run](https://run.claw.cloud).

> **Status:** Dry-run mode is fully operational. Live order placement (CLOB signing) is not yet implemented — see [Future Work](#future-work).

## Architecture

Kudan runs a 3-stage async pipeline with zero threads:

```
Discovery loop (every DISCOVERY_POLL_SECONDS)
  └─ Fetches tweet + crypto events from Polymarket Gamma API
  └─ Classifies into buckets: 5min / 15min / hourly / 4hour / daily / weekly / monthly
  └─ Incremental refresh: full fetch once per new event, lightweight refresh for existing

Per-bucket polling tasks (one task per bucket, independent intervals)
  └─ Applies 99% probability threshold (configurable)
  └─ Tweet safety: rejects markets too close to tweet-count range boundaries
  └─ Crypto safety: fetches live BTCUSDT from Binance, checks % distance to price boundary
  └─ Liquidity + slippage guards via RiskManager
  └─ Pushes valid opportunities to asyncio.PriorityQueue (nearest resolution first)

Execution consumer (single task)
  └─ Dequeues in urgency order
  └─ DRY_RUN=true → logs to SQLite, sends Telegram alert, no real order
  └─ DRY_RUN=false → TODO (CLOB signed order placement not yet implemented)
```

## Quick Start

```bash
cp .env.example .env
# edit .env — minimum required: POLYMARKET_PRIVATE_KEY, POLYMARKET_WALLET_ADDRESS, POLYGON_RPC_PRIMARY

# local run (Windows venv)
.venv\Scripts\python.exe main.py

# health check
curl http://127.0.0.1:8080/health
```

Set `DRY_RUN=true` in `.env` until live execution is validated.

## Docker

```bash
docker build -t kudan .
docker run --env-file .env kudan
```

Or with Compose (if `docker-compose.yml` present):

```bash
docker compose up -d --build
docker compose logs -f kudan
```

## Health Endpoint

`GET /health` returns:

```json
{
  "status": "ok",
  "api_ok": true,
  "rpc_ok": true,
  "last_market_scan": "2026-04-30T12:00:00+00:00",
  "candidate_count": 12,
  "queue_depth": 0,
  "circuit_open": false,
  "recent_failures": 0,
  "dashboard": {
    "scanned_markets": 240,
    "opportunities_found": 3,
    "trades_sent": 0
  }
}
```

`status` is `"degraded"` when either `api_ok` or `rpc_ok` is false.

## Circuit Breaker

After `CIRCUIT_BREAKER_THRESHOLD` failures within `CIRCUIT_BREAKER_WINDOW_SECONDS`, all polling tasks pause for `CIRCUIT_BREAKER_OPEN_SECONDS`. A Telegram alert fires on open. Defaults: 3 failures / 60s window / 60s pause.

## SQLite Schema

DB at `DB_PATH` (default `./kudan.db` locally, `/data/kudan.db` in container):

| Table | Purpose |
|-------|---------|
| `candidate_events` | In-memory snapshot of shortlisted events (crash recovery) |
| `filtered_events` | Pre-bucket-match classified event log |
| `opportunities` | Every opportunity detected by strategy |
| `trades` | Every order attempted (dry-run or live) |
| `scan_log` | Raw market scan payloads |
| `positions` | Open position tracking |

## Supported Event Types

| Type | Identifier | Data source |
|------|-----------|-------------|
| Elon tweet count | Tag ID `972`, ticker contains `elon-musk-of-tweets` | `tweetCount` from Gamma API |
| Bitcoin price | Tag IDs `235` + `1312` | Live BTCUSDT via Polymarket RTDS WebSocket (`wss://ws-live-data.polymarket.com`) |

## RPC Failover

`POLYGON_RPC_PRIMARY` + `POLYGON_RPC_FALLBACKS` (comma-separated) feed `RpcRotator`, which round-robins on failure. Free providers like `1rpc.io/matic` and `polygon-rpc.com` work as fallbacks.

## Deployment

See [clawcloud-deployment.md](clawcloud-deployment.md) for the ClawCloud Run guide.
See [TESTING_LOCAL.md](TESTING_LOCAL.md) for local test instructions.

## Future Work

- Implement Polymarket CLOB signed order placement in `execution/trader.py`
- On-chain position reconciliation and live bankroll discovery
- AI-assisted probability confidence scoring
