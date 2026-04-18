from __future__ import annotations

import asyncio
import contextlib
import os
import signal
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

import aiohttp
import uvicorn
from fastapi import FastAPI
from web3 import HTTPProvider, Web3

from config import AppSettings, load_settings
from data import ClobClient, EventFetcher, GammaClient
from data.auth import ClobAuthError
from data.rate_limits import RateLimiterRegistry
from db import SqliteStore
from execution import Redeemer, TradeExecutor
from monitoring import Dashboard, HealthState, TelegramAlerter, setup_logging
from strategies.high_prob import HighProbabilityStrategy
from utils.risk import RiskManager
from utils.rpc import RpcRotator
from utils.vpn import OpenVpnController


@dataclass(slots=True)
class AdaptiveInterval:
    """Adjusts loop intervals dynamically to keep idle workloads lightweight."""

    base_seconds: float
    max_scale: float
    recovery: float
    current_scale: float = 1.0

    def next_timeout(self, had_opportunity: bool) -> float:
        """Returns next sleep duration while shrinking latency after signals appear."""
        if had_opportunity:
            self.current_scale = max(1.0, self.current_scale * self.recovery)
        else:
            self.current_scale = min(self.max_scale, self.current_scale * 1.15)
        return max(self.base_seconds * self.current_scale, 1.0)


def build_health_app(health: HealthState, dashboard: Dashboard) -> FastAPI:
    """Builds a minimal health endpoint for ClawCloud checks."""
    app = FastAPI(title="Kudan Health", version="1.0.0")

    @app.get("/health")
    async def health_check() -> dict[str, Any]:
        return {
            "status": "ok" if health.api_ok and health.rpc_ok else "degraded",
            "api_ok": health.api_ok,
            "rpc_ok": health.rpc_ok,
            "vpn_ok": health.vpn_ok,
            "last_market_scan": health.last_market_scan_ts.isoformat(),
            "dashboard": {
                "scanned_markets": dashboard.scanned_markets,
                "opportunities_found": dashboard.opportunities_found,
                "trades_sent": dashboard.trades_sent,
            },
        }

    return app


async def ensure_rpc_health(settings: AppSettings, rpc_rotator: RpcRotator, logger, health: HealthState) -> None:
    """Checks Polygon RPC health and rotates to fallbacks on failures."""
    for _ in range(1 + len(settings.polygon_rpc_fallbacks)):
        rpc_url = rpc_rotator.current()

        def _check() -> int:
            return Web3(HTTPProvider(rpc_url)).eth.block_number

        try:
            block = await asyncio.to_thread(_check)
            logger.debug("RPC healthy at %s (block %s)", rpc_url, block)
            health.rpc_ok = True
            return
        except Exception as exc:
            logger.warning("RPC check failed for %s: %s", rpc_url, exc)
            rpc_rotator.next()

    health.rpc_ok = False


async def looping_task(
    name: str,
    interval_seconds: int,
    work: Callable[[], Awaitable[int]],
    logger,
    stop_event: asyncio.Event,
    max_scale: float,
    recovery: float,
) -> None:
    """Runs one repeating task with adaptive sleep and graceful stop behavior."""
    adaptive = AdaptiveInterval(base_seconds=interval_seconds, max_scale=max_scale, recovery=recovery)
    logger.info("Starting task %s every ~%ss", name, interval_seconds)
    while not stop_event.is_set():
        started = datetime.now(timezone.utc)
        found = 0
        try:
            found = await work()
        except Exception as exc:
            logger.exception("Task %s failed: %s", name, exc)

        elapsed = (datetime.now(timezone.utc) - started).total_seconds()
        sleep_for = max(adaptive.next_timeout(had_opportunity=found > 0) - elapsed, 1)
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=sleep_for)
        except asyncio.TimeoutError:
            pass


async def serve_health_api(app: FastAPI, host: str, port: int, logger, stop_event: asyncio.Event) -> None:
    """Runs in-process health API server and exits when stop is requested."""
    config = uvicorn.Config(app, host=host, port=port, log_level="warning", lifespan="off")
    server = uvicorn.Server(config)
    server.install_signal_handlers = lambda: None

    task = asyncio.create_task(server.serve())
    logger.info("Health endpoint listening at http://%s:%s/health", host, port)
    await stop_event.wait()
    server.should_exit = True
    with contextlib.suppress(asyncio.CancelledError):
        await task


async def main() -> None:
    """Bootstraps services and runs discovery plus bucket-aware high-prob loops."""
    settings = load_settings()
    logger = setup_logging(settings.log_level)
    health = HealthState()
    dashboard = Dashboard()
    stop_event = asyncio.Event()

    def _request_stop(signum: int, _frame: Any) -> None:
        logger.warning("Signal %s received. Kudan begins graceful shutdown.", signum)
        stop_event.set()

    if hasattr(signal, "SIGTERM"):
        signal.signal(signal.SIGTERM, _request_stop)
    if hasattr(signal, "SIGINT"):
        signal.signal(signal.SIGINT, _request_stop)

    store = SqliteStore(settings.db_path)
    await store.init()

    risk = RiskManager(
        bankroll_usd=10_000.0,
        max_bankroll_exposure_pct=settings.max_bankroll_exposure_pct,
        max_trade_exposure_pct=settings.max_trade_exposure_pct,
        min_liquidity_usd=settings.min_liquidity_usd,
        max_slippage_pct=settings.max_slippage_pct,
    )
    alerts = TelegramAlerter(settings.telegram_bot_token, settings.telegram_chat_id, logger)
    trader = TradeExecutor(
        dry_run=settings.dry_run,
        wallet_address=settings.polymarket_wallet_address,
        private_key=settings.polymarket_private_key,
        logger=logger,
        store=store,
        alerts=alerts,
    )
    redeemer = Redeemer(logger, alerts)
    rpc_rotator = RpcRotator(settings.polygon_rpc_primary, settings.polygon_rpc_fallbacks)
    vpn = OpenVpnController(
        enabled=settings.vpn_enabled,
        config_file=settings.openvpn_config_file,
        reconnect_seconds=settings.vpn_reconnect_seconds,
        openvpn_executable=settings.openvpn_executable,
        auth_file=settings.openvpn_auth_file,
    )
    strategy = HighProbabilityStrategy(probability_threshold=settings.high_prob_threshold)

    health_host = os.getenv("HEALTH_HOST", "0.0.0.0")
    health_port = int(os.getenv("HEALTH_PORT", "8080"))
    idle_interval_max_scale = float(os.getenv("IDLE_INTERVAL_MAX_SCALE", "2.5"))
    active_interval_recovery = float(os.getenv("ACTIVE_INTERVAL_RECOVERY", "0.65"))
    discovery_seconds = int(os.getenv("DISCOVERY_POLL_SECONDS", "600"))

    # Required workflow poll intervals.
    bucket_intervals = {
        "5min": int(os.getenv("BUCKET_5MIN_SECONDS", "20")),
        "hourly": int(os.getenv("BUCKET_HOURLY_SECONDS", "45")),
        "daily": int(os.getenv("BUCKET_DAILY_SECONDS", "60")),
        "weekly": int(os.getenv("BUCKET_WEEKLY_SECONDS", "300")),
        "monthly": int(os.getenv("BUCKET_MONTHLY_SECONDS", "300")),
    }

    health_app = build_health_app(health, dashboard)

    async with aiohttp.ClientSession(headers={"User-Agent": "Kudan/0.1"}) as session:
        shared_limiters = RateLimiterRegistry()
        gamma = GammaClient(settings.gamma_base_url, session, logger, rate_limiter_registry=shared_limiters)
        clob = ClobClient(
            base_url=settings.clob_base_url,
            session=session,
            logger=logger,
            chain_id=settings.polymarket_chain_id,
            private_key=settings.polymarket_private_key,
            api_key=settings.clob_api_key,
            api_secret=settings.clob_api_secret,
            api_passphrase=settings.clob_api_passphrase,
            rate_limiter_registry=shared_limiters,
        )
        event_fetcher = EventFetcher(gamma=gamma, store=store, logger=logger)

        try:
            await clob.ensure_authenticated_session()
            logger.info("CLOB authentication layer is ready (L2 credentials loaded)")
        except ClobAuthError as exc:
            logger.warning(
                "CLOB auth bootstrap unavailable: %s. Continuing in public read-only mode; "
                "authenticated endpoints will fail until connectivity recovers.",
                exc,
            )

        async def discovery_work() -> int:
            grouped = await event_fetcher.fetch_relevant_events()
            health.heartbeat()
            health.api_ok = True
            discovered = sum(len(items) for items in grouped.values())
            logger.info("Discovery shortlisted %s candidate events", discovered)
            return 1 if discovered > 0 else 0

        async def bucket_work(bucket: str) -> int:
            best = await strategy.scan_high_prob_candidates(
                bucket=bucket,
                event_fetcher=event_fetcher,
                clob=clob,
                risk=risk,
                store=store,
                trader=trader,
                logger=logger,
            )
            if best is None:
                return 0
            dashboard.opportunities_found += 1
            if not trader.dry_run:
                dashboard.trades_sent += 1
            return 1

        async def health_work() -> int:
            await ensure_rpc_health(settings, rpc_rotator, logger, health)
            logger.info(dashboard.as_line())
            return 0

        async def redeem_work() -> int:
            await redeemer.auto_redeem()
            return 0

        tasks = [
            asyncio.create_task(
                looping_task(
                    "event_discovery",
                    discovery_seconds,
                    discovery_work,
                    logger,
                    stop_event,
                    max_scale=1.0,
                    recovery=1.0,
                )
            ),
            asyncio.create_task(
                looping_task(
                    "health",
                    60,
                    health_work,
                    logger,
                    stop_event,
                    max_scale=1.0,
                    recovery=1.0,
                )
            ),
            asyncio.create_task(
                looping_task(
                    "redeem",
                    600,
                    redeem_work,
                    logger,
                    stop_event,
                    max_scale=1.0,
                    recovery=1.0,
                )
            ),
            asyncio.create_task(serve_health_api(health_app, health_host, health_port, logger, stop_event)),
        ]

        for bucket, interval in bucket_intervals.items():
            tasks.append(
                asyncio.create_task(
                    looping_task(
                        f"high_prob_{bucket}",
                        interval,
                        lambda b=bucket: bucket_work(b),
                        logger,
                        stop_event,
                        max_scale=idle_interval_max_scale,
                        recovery=active_interval_recovery,
                    )
                )
            )

        if settings.vpn_enabled:
            tasks.append(asyncio.create_task(vpn.watch_loop(logger, stop_event)))

        await alerts.send("Kudan awakened: guardian of hidden probabilities now watches the oracle realm.")

        try:
            await asyncio.gather(*tasks)
        except asyncio.CancelledError:
            logger.info("Cancellation received. Shutting down tasks.")
        finally:
            stop_event.set()
            for task in tasks:
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await task
            await alerts.send("Kudan sleeping: graceful shutdown complete.")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, asyncio.CancelledError):
        pass
