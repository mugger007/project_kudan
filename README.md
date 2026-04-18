# Project Kudan

Kudan is the guardian of hidden probabilities and forger of risk-free edges in the oracle realm.

This repository is a production-ready starter for a free-tier, always-on Polymarket bot focused on:

- High-Probability entries: event-driven tweet and crypto candidate workflow with bucket-aware checks.

The stack is now optimized for ClawCloud Run deployment through App Launchpad using a container image.

## Core Features

- Async multi-loop scanner with adaptive intervals for lower idle CPU usage.
- FastAPI health endpoint at /health for ClawCloud checks.
- Gamma market ingestion + CLOB order book checks.
- Telegram alerts, SQLite logging, and RPC failover.
- Optional OpenVPN startup and auto-reconnect loop using Proton .ovpn configs.

## Updated Structure

.
|- main.py
|- entrypoint.sh
|- requirements.txt
|- .env.example
|- Dockerfile
|- docker-compose.yml
|- clawcloud-deployment.md
|- config/
|  |- __init__.py
|  |- buckets.py
|  |- settings.py
|- data/
|  |- __init__.py
|  |- cache.py
|  |- clob_client.py
|  |- event_fetcher.py
|  |- gamma_client.py
|  |- models.py
|- db/
|  |- __init__.py
|  |- sqlite_store.py
|- strategies/
|  |- __init__.py
|  |- base.py
|  |- high_prob.py
|- execution/
|  |- __init__.py
|  |- order_builder.py
|  |- redeem.py
|  |- trader.py
|- monitoring/
|  |- __init__.py
|  |- alerts.py
|  |- dashboard.py
|  |- health.py
|  |- logger.py
|- utils/
|  |- __init__.py
|  |- retry.py
|  |- risk.py
|  |- rpc.py
|  |- vpn.py
|- systemd/
   |- kudan.service

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

## High-Probability Tweet and Crypto Workflow

Kudan now uses a single high-probability strategy pipeline:

1. Periodic event discovery every 5-15 minutes from Gamma /events/keyset with cursor paging.
2. Parallel relevance filters:
   - Tweet markets: tag_id=97 plus Elon tweet series matching.
   - Crypto markets: BTC/ETH/SOL and range-oriented title/ticker filters.
3. Bucket classification and prefiltering into 5min/hourly/daily/weekly/monthly.
4. Candidate snapshot caching into SQLite candidate_events table.
5. Per-bucket scanners refresh each event, enforce 99% checks, apply tweet boundary safety, then select one BestMarket and execute.

Bucket intervals:

- 5min: 15-30 seconds
- hourly: 30-60 seconds
- daily: 60 seconds
- weekly/monthly: 5 minutes

For tweet events, markets within plus/minus 10 tweets of boundaries are rejected.
Remaining markets are ranked by safety margin distance from boundaries.

## Local Docker Test

docker compose up -d --build
docker compose logs -f kudan
curl http://127.0.0.1:8080/health

## OpenVPN (Proton Config) in Container

- Set VPN_ENABLED=true and OPENVPN_CONFIG_FILE.
- Optional: set OPENVPN_EXECUTABLE (for example `openvpn` or `openvpn-gui.exe`).
- Optional: set OPENVPN_AUTH_FILE if your .ovpn requires auth-user-pass credentials.
- Ensure container has NET_ADMIN capability and TUN access.
- If VPN is disabled, entrypoint drops privileges and runs as non-root user.
- If VPN is enabled, container may run as root depending on network requirements.

## Security Notes

- Never hardcode wallet keys.
- Use a dedicated hot wallet with small funds.
- Keep DRY_RUN=true until signed order execution is fully validated.

## Future TODOs

- Implement full Polymarket CLOB signing and order lifecycle management.
- Add on-chain position reconciliation and live bankroll discovery.
- Add AI-assisted probability models for signal confidence scoring.

In the oracle storms, Kudan stands watch and tempers edge into certainty.
