from __future__ import annotations

import asyncio
import contextlib
import json
from typing import Any

import aiohttp
import uvicorn
from fastapi import FastAPI

from data.event_fetcher import CandidateEvent
from db.sqlite_store import SqliteStore
from monitoring.health import HealthState
from monitoring.dashboard import Dashboard


def build_health_app(
    health: HealthState,
    dashboard: Dashboard,
    candidate_events: dict[str, dict[str, Any]],
    opportunities_queue: asyncio.PriorityQueue[tuple[float, dict[str, Any]]],
    scheduler_state: dict[str, Any] | None = None,
) -> FastAPI:
    app = FastAPI(title="Kudan Health", version="1.0.0")

    @app.get("/health")
    async def health_check() -> dict[str, Any]:
        state = scheduler_state or {}
        loop = asyncio.get_running_loop()
        circuit_open_until = float(state.get("circuit_open_until", 0.0) or 0.0)
        circuit_open = circuit_open_until > loop.time()
        return {
            "status": "ok" if health.api_ok and health.rpc_ok else "degraded",
            "api_ok": health.api_ok,
            "rpc_ok": health.rpc_ok,
            "last_market_scan": health.last_market_scan_ts.isoformat(),
            "candidate_count": len(candidate_events),
            "queue_depth": opportunities_queue.qsize(),
            "circuit_open": circuit_open,
            "recent_failures": int(state.get("recent_failures", 0) or 0),
            "dashboard": {
                "scanned_markets": dashboard.scanned_markets,
                "opportunities_found": dashboard.opportunities_found,
                "trades_sent": dashboard.trades_sent,
            },
        }

    return app


async def wait_for_http_endpoint(
    session: aiohttp.ClientSession,
    url: str,
    logger,
    *,
    name: str,
    attempts: int = 6,
    base_delay: float = 2.0,
) -> None:
    last_error: Exception | None = None
    for attempt in range(1, attempts + 1):
        try:
            async with session.get(url, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                if resp.status < 500:
                    logger.info("%s endpoint is reachable (%s)", name, resp.status)
                    return
                last_error = RuntimeError(f"{name} returned HTTP {resp.status}")
        except Exception as exc:
            last_error = exc

        if attempt < attempts:
            delay = base_delay * (2 ** (attempt - 1))
            logger.warning(
                "Waiting for %s endpoint (%s/%s): %s; retry in %ss",
                name,
                attempt,
                attempts,
                last_error,
                delay,
            )
            await asyncio.sleep(delay)

    raise RuntimeError(f"{name} endpoint remained unreachable after {attempts} attempts: {last_error}")


async def serve_health_api(app: FastAPI, host: str, port: int, logger, stop_event: asyncio.Event) -> None:
    config = uvicorn.Config(app, host=host, port=port, log_level="warning", lifespan="off")
    server = uvicorn.Server(config)
    server.install_signal_handlers = lambda: None

    task = asyncio.create_task(server.serve())
    logger.info("Health endpoint listening at http://%s:%s/health", host, port)
    await stop_event.wait()
    server.should_exit = True
    with contextlib.suppress(asyncio.CancelledError):
        await task


def extract_token_ids(market: dict[str, Any]) -> tuple[str, str] | None:
    raw = market.get("clobTokenIds")
    if isinstance(raw, list) and len(raw) >= 2:
        return str(raw[0]), str(raw[1])
    if isinstance(raw, str) and raw:
        try:
            parsed = json.loads(raw)
            if isinstance(parsed, list) and len(parsed) >= 2:
                return str(parsed[0]), str(parsed[1])
        except json.JSONDecodeError:
            return None
    return None


def candidate_row(candidate: CandidateEvent) -> dict[str, Any]:
    return {
        "event_id": candidate.event_id,
        "title": candidate.title,
        "endDate": candidate.endDate,
        "tweetCount": candidate.tweetCount,
        "event_type": "tweet",
        "current_price": None,
        "bucket": candidate.bucket,
        "raw_data": candidate.raw_data,
    }


async def persist_candidate_snapshot(
    store: SqliteStore,
    candidate_events: dict[str, dict[str, Any]],
    logger,
) -> None:
    rows = list(candidate_events.values())
    await store.replace_candidate_events(rows)
    logger.debug("Candidate snapshot persisted: %s events", len(rows))


async def load_candidate_snapshot(
    store: SqliteStore,
    buckets: dict[str, int],
) -> dict[str, dict[str, Any]]:
    restored: dict[str, dict[str, Any]] = {}
    for bucket in buckets:
        rows = await store.list_candidate_events(bucket)
        for row in rows:
            event_id = str(row.get("event_id") or "")
            if not event_id:
                continue
            restored[event_id] = {
                "event_id": event_id,
                "title": str(row.get("title") or event_id),
                "endDate": str(row.get("endDate") or ""),
                "tweetCount": row.get("tweetCount"),
                "event_type": str(row.get("event_type") or "tweet"),
                "current_price": row.get("current_price"),
                "bucket": str(row.get("bucket") or bucket),
                "raw_data": row.get("raw_data") or {},
            }
    return restored
