# Project Kudan

This repository is a production-ready starter for a free-tier, always-on Polymarket bot focused on:

- High-Probability entries: event-driven tweet and crypto candidate workflows with bucket-aware checks.

The stack is now optimized for ClawCloud Run deployment through App Launchpad using a container image.

## Core Features

- Pure asyncio 3-stage scheduler (discovery, per-bucket polling, execution consumer) with zero threads.
- Priority-first opportunity execution using asyncio.PriorityQueue (shortest time-to-resolution first).
- Incremental candidate refresh for low latency: full fetch only for new events, lightweight refresh for existing events.
- Back-pressure guard: bucket polling auto-slows when queue depth exceeds threshold.
- Circuit breaker: pauses scheduler after repeated failures and emits Telegram alert.
- FastAPI health endpoint at /health with queue depth, candidate count, and circuit state.
- SQLite persistence for scans/opportunities/trades plus candidate snapshot crash recovery.

## ClawCloud Setup

1. Sign up at https://run.claw.cloud with GitHub login.
2. Claim monthly free credit (typically around $5 with an eligible GitHub account).
3. Push image to GitHub Container Registry:

   docker login ghcr.io
   docker build -t ghcr.io/<your-user>/kudan:latest .
   docker push ghcr.io/<your-user>/kudan:latest

4. In App Launchpad, create a new app from image:
   - Image: ghcr.io/<your-user>/kudan:latest
   - Port: 8080
   - Health path: /health
   - Suggested resources: 1-2 vCPU, 2-4 GB RAM

5. Configure environment variables from .env.example in ClawCloud dashboard.

6. Ensure persistent volume mount for /data if available so SQLite survives restarts.

Detailed guide: see clawcloud-deployment.md.

## High-Probability Discovery Workflow

Kudan uses a latency-first 3-stage pipeline:

1. Discovery stage:
   - Runs every DISCOVERY_POLL_SECONDS.
   - Fetches relevant tweet and crypto events.
   - Adds only new events with one-time full detail fetch.
   - Refreshes existing candidates incrementally (tweetCount for tweet events, current_price for crypto events).

2. Per-bucket polling stage:
   - One long-running task per bucket (5min/15min/hourly/4hour/daily/weekly/monthly).
   - Scans only matching in-memory candidates.
   - Applies 99% threshold, liquidity/slippage checks, and tweet/crypto safety rules.
   - Pushes valid opportunities to a PriorityQueue by remaining seconds to event end.

3. Execution stage:
   - Consumes opportunities in urgency order (nearest resolution first).
   - Executes immediately and logs outcomes to SQLite.
   - Sends Telegram alerts via execution pipeline.

Candidate schema in SQLite candidate_events now supports mixed event types:

- event_type: tweet or crypto
- tweetCount: used for tweet events
- current_price: used for crypto events

## Local Docker Test

docker compose up -d --build
docker compose logs -f kudan
curl http://127.0.0.1:8080/health

## Security Notes

- Never hardcode wallet keys.
- Use a dedicated hot wallet with small funds.
- Keep DRY_RUN=true until signed order execution is fully validated.

## Future TODOs

- Implement full Polymarket CLOB signing and order lifecycle management.
- Add on-chain position reconciliation and live bankroll discovery.
- Add AI-assisted probability models for signal confidence scoring.

In the oracle storms, Kudan stands watch and tempers edge into certainty.
