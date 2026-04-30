from __future__ import annotations

import asyncio
import contextlib
import os
import signal
import socket
from typing import Any

import aiohttp

from config import load_settings
from data import CandidateEvent, ClobClient, EventFetcher, GammaClient
from data.auth import ClobAuthError
from data.rate_limits import RateLimiterRegistry
from data.price_feed import BtcPriceFeed
from db import SqliteStore
from execution import TradeExecutor
from execution.execute_trade import execute_trade
from monitoring import Dashboard, HealthState, TelegramAlerter, setup_logging
from strategies import HighProbabilityStrategy
from utils.risk import RiskManager
from utils.runtime_helpers import (
    build_health_app,
    candidate_row,
    load_candidate_snapshot,
    persist_candidate_snapshot,
    serve_health_api,
    wait_for_http_endpoint,
)
from utils.scheduler_helpers import (
    CircuitBreaker,
    bucket_time_match,
    classify_event_bucket,
    event_type_for_event,
    include_event,
    remaining_seconds,
)


async def main() -> None:
    """Bootstraps runtime services and executes the async 3-stage scheduler."""
    settings = load_settings()
    logger = setup_logging(settings.log_level)
    health = HealthState()
    dashboard = Dashboard()
    stop_event = asyncio.Event()

    discovery_poll_seconds = settings.discovery_poll_seconds
    bucket_intervals: dict[str, int] = {
        "5min": settings.bucket_5min_seconds,
        "15min": settings.bucket_15min_seconds,
        "hourly": settings.bucket_1hour_seconds,
        "4hour": settings.bucket_4hour_seconds,
        "daily": settings.bucket_daily_seconds,
        "weekly": settings.bucket_weekly_seconds,
        "monthly": settings.bucket_monthly_seconds,
    }

    def _request_stop(signum: int, _frame: Any) -> None:
        """Converts OS termination signals into scheduler stop events."""
        logger.warning("Signal %s received. Kudan begins graceful shutdown.", signum)
        stop_event.set()

    if hasattr(signal, "SIGTERM"):
        signal.signal(signal.SIGTERM, _request_stop)
    if hasattr(signal, "SIGINT"):
        signal.signal(signal.SIGINT, _request_stop)

    store = SqliteStore(settings.db_path)
    await store.init()

    risk = RiskManager(
        bankroll_usd=settings.bankroll_usd,
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
    strategy = HighProbabilityStrategy(probability_threshold=settings.high_prob_threshold)

    price_feed = BtcPriceFeed(logger)
    candidate_events: dict[str, dict[str, Any]] = await load_candidate_snapshot(store, bucket_intervals)
    opportunities_queue: asyncio.PriorityQueue[tuple[float, dict[str, Any]]] = asyncio.PriorityQueue()
    circuit_breaker = CircuitBreaker(
        logger,
        alerts,
        threshold=settings.circuit_breaker_threshold,
        window_seconds=settings.circuit_breaker_window_seconds,
        open_seconds=settings.circuit_breaker_open_seconds,
    )

    health_host = os.getenv("HEALTH_HOST", "0.0.0.0")
    health_port = int(os.getenv("HEALTH_PORT", "8080"))

    connector = aiohttp.TCPConnector(
        family=socket.AF_INET,
        ttl_dns_cache=30,
        enable_cleanup_closed=True,
    )

    async with aiohttp.ClientSession(headers={"User-Agent": "Kudan/0.1"}, connector=connector) as session:
        await wait_for_http_endpoint(session, f"{settings.gamma_base_url.rstrip('/')}/", logger, name="Gamma")
        await wait_for_http_endpoint(session, f"{settings.clob_base_url.rstrip('/')}/", logger, name="CLOB")

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

        event_fetcher = EventFetcher(
            gamma=gamma,
        )

        try:
            await clob.ensure_authenticated_session()
            logger.info("CLOB authentication layer is ready (L2 credentials loaded)")
        except ClobAuthError as exc:
            logger.warning(
                "CLOB auth bootstrap unavailable: %s. Continuing in public read-only mode; "
                "authenticated endpoints will fail until connectivity recovers.",
                exc,
            )

        async def discovery_task() -> None:
            """Discovers and incrementally refreshes candidate events at fixed intervals."""
            logger.info("Discovery task started (interval=%ss)", discovery_poll_seconds)
            while not stop_event.is_set():
                try:
                    if await circuit_breaker.wait_if_open():
                        continue

                    events = await event_fetcher.fetch_events()
                    discovered = 0
                    newly_added = 0
                    updated_existing = 0
                    current_price_btc: float = price_feed.latest_price
                    if current_price_btc > 0:
                        logger.info("Discovery using BTC price: %.2f USDT", current_price_btc)
                    else:
                        logger.warning("BTC price feed not ready (latest_price=0); crypto safety checks will reject all crypto markets this cycle")
                    filtered_events: list[dict[str, str]] = []
                    seen_event_ids: set[str] = set()

                    for event in events:
                        if not include_event(event):
                            continue

                        event_id = str(event.get("id") or "")
                        if not event_id or event_id in seen_event_ids:
                            continue
                        seen_event_ids.add(event_id)

                        bucket = classify_event_bucket(event)
                        if not bucket:
                            continue

                        event_type = event_type_for_event(event)
                        if event_type not in {"tweet", "crypto"}:
                            continue

                        # Keep filtered_events as pre-bucket-time shortlist visibility.
                        filtered_events.append(
                            {
                                "event_id": event_id,
                                "title": str(event.get("title") or event_id),
                                "classification": bucket,
                            }
                        )
                        if not bucket_time_match(event, bucket):
                            continue

                        discovered += 1
                        item = CandidateEvent(
                            event_id=event_id,
                            title=str(event.get("title") or event_id),
                            endDate=str(event.get("endDate") or ""),
                            tweetCount=event.get("tweetCount"),
                            event_type=event_type,
                            current_price=current_price_btc if event_type == "crypto" else None,
                            bucket=bucket,
                            raw_data=event,
                        )

                        refreshed_current_price = item.current_price
                        refreshed_tweet_count = item.tweetCount if event_type == "tweet" else None

                        if item.event_id in candidate_events:
                            # Incremental refresh for existing entries: no full event pull.
                            existing = candidate_events[item.event_id]
                            existing["title"] = item.title or existing.get("title")
                            existing["endDate"] = item.endDate or existing.get("endDate")
                            existing["bucket"] = bucket
                            existing["event_type"] = event_type
                            existing["tweetCount"] = refreshed_tweet_count
                            existing["current_price"] = refreshed_current_price
                            updated_existing += 1
                            continue

                        # New events fetch full details once, then cache.
                        full_event = await event_fetcher.refresh_event(item.event_id)
                        row_source = full_event if isinstance(full_event, dict) and full_event else item.raw_data
                        row = candidate_row(item)
                        row["raw_data"] = row_source
                        candidate_events[item.event_id] = row
                        newly_added += 1

                    await store.replace_filtered_events(filtered_events)

                    await persist_candidate_snapshot(store, candidate_events, logger)
                    health.heartbeat()
                    health.api_ok = True
                    logger.info(
                        "Discovery cycle complete: discovered=%s new=%s refreshed=%s in_memory=%s",
                        discovered,
                        newly_added,
                        updated_existing,
                        len(candidate_events),
                    )
                except Exception as exc:
                    health.api_ok = False
                    logger.exception("Discovery task failed: %s", exc)
                    await circuit_breaker.record_failure("discovery", exc)

                try:
                    await asyncio.wait_for(stop_event.wait(), timeout=discovery_poll_seconds)
                except asyncio.TimeoutError:
                    pass

        async def bucket_polling_task(bucket: str, interval_seconds: int) -> None:
            """Polls one bucket for opportunities and enqueues highest-priority candidates."""
            logger.info("Bucket task started: %s (interval=%ss)", bucket, interval_seconds)
            while not stop_event.is_set():
                try:
                    if await circuit_breaker.wait_if_open():
                        continue

                    if opportunities_queue.qsize() > settings.queue_backpressure_threshold:
                        delay = min(interval_seconds, settings.queue_backpressure_sleep_seconds)
                        logger.warning(
                            "Back-pressure active for bucket=%s queue_depth=%s; slowing polling by %ss",
                            bucket,
                            opportunities_queue.qsize(),
                            delay,
                        )
                        await asyncio.sleep(delay)
                        continue

                    logger.info(
                        "Bucket task tick: bucket=%s candidate_count=%s queue_depth=%s",
                        bucket,
                        sum(1 for event_data in candidate_events.values() if str(event_data.get("bucket") or "") == bucket),
                        opportunities_queue.qsize(),
                    )

                    bucket_event_ids = [
                        event_id
                        for event_id, event_data in candidate_events.items()
                        if str(event_data.get("bucket") or "") == bucket
                    ]

                    dashboard.scanned_markets += len(bucket_event_ids)

                    for event_id in bucket_event_ids:
                        if event_id not in candidate_events:
                            continue

                        opportunity = await strategy.evaluate_event_opportunity(
                            event_id=event_id,
                            candidate_events=candidate_events,
                            event_fetcher=event_fetcher,
                            clob=clob,
                            risk=risk,
                            btc_price=price_feed.latest_price,
                            classify_event_bucket=classify_event_bucket,
                            event_type_for_event=event_type_for_event,
                        )
                        if not opportunity:
                            continue

                        await store.log_opportunity(
                            market_id=opportunity["market_id"],
                            strategy=opportunity["strategy"],
                            side=opportunity["side"],
                            edge=float(opportunity["edge"]),
                            confidence=float(opportunity["confidence"]),
                            metadata={
                                "event_id": opportunity["event_id"],
                                "bucket": opportunity["bucket"],
                                "safety_margin": opportunity["safety_margin"],
                                "price": opportunity["price"],
                                "size": opportunity["size"],
                            },
                        )

                        await opportunities_queue.put((remaining_seconds(opportunity.get("endDate")), opportunity))
                        dashboard.opportunities_found += 1
                        candidate_events.pop(event_id, None)

                    if bucket_event_ids:
                        await persist_candidate_snapshot(store, candidate_events, logger)
                except Exception as exc:
                    logger.exception("Bucket task failed for %s: %s", bucket, exc)
                    await circuit_breaker.record_failure(f"bucket:{bucket}", exc)

                try:
                    await asyncio.wait_for(stop_event.wait(), timeout=interval_seconds)
                except asyncio.TimeoutError:
                    pass

        async def execution_consumer_task() -> None:
            """Consumes prioritized opportunities and executes trades in urgency order."""
            logger.info("Execution consumer started")
            while not stop_event.is_set() or not opportunities_queue.empty():
                try:
                    if await circuit_breaker.wait_if_open():
                        continue

                    _priority, opportunity = await asyncio.wait_for(opportunities_queue.get(), timeout=1.0)
                except asyncio.TimeoutError:
                    continue

                try:
                    await execute_trade(
                        event_id=str(opportunity["event_id"]),
                        market_id=str(opportunity["market_id"]),
                        token_id=str(opportunity["token_id"]),
                        side=str(opportunity["side"]),
                        price=float(opportunity["price"]),
                        size=float(opportunity["size"]),
                        strategy=str(opportunity["strategy"]),
                        trader=trader,
                    )
                    dashboard.trades_sent += 1
                except Exception as exc:
                    logger.exception("Execution failed for event=%s market=%s: %s", opportunity.get("event_id"), opportunity.get("market_id"), exc)
                    await circuit_breaker.record_failure("execution", exc)
                finally:
                    opportunities_queue.task_done()

        health_app = build_health_app(health, dashboard, candidate_events, opportunities_queue, circuit_breaker.state)

        tasks = [
            asyncio.create_task(price_feed.run(stop_event), name="btc_price_feed"),
            asyncio.create_task(discovery_task(), name="discovery"),
            asyncio.create_task(execution_consumer_task(), name="execution_consumer"),
            asyncio.create_task(serve_health_api(health_app, health_host, health_port, logger, stop_event), name="health_api"),
        ]
        for bucket, interval_seconds in bucket_intervals.items():
            tasks.append(asyncio.create_task(bucket_polling_task(bucket, interval_seconds), name=f"bucket_{bucket}"))

        await alerts.send("Kudan awakened: high-speed async scheduler is active.")

        try:
            await asyncio.gather(*tasks)
        except asyncio.CancelledError:
            logger.info("Cancellation received. Shutting down tasks.")
        finally:
            stop_event.set()
            with contextlib.suppress(Exception):
                await asyncio.wait_for(opportunities_queue.join(), timeout=15)

            for task in tasks:
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await task

            await persist_candidate_snapshot(store, candidate_events, logger)
            await alerts.send("Kudan sleeping: graceful shutdown complete.")


if __name__ == "__main__":
    try:
        if os.name == "nt" and hasattr(asyncio, "WindowsSelectorEventLoopPolicy"):
            asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
        asyncio.run(main())
    except (KeyboardInterrupt, asyncio.CancelledError):
        pass
