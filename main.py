from __future__ import annotations

import asyncio
import contextlib
import os
import signal
import socket
from typing import Any

import aiohttp

from config import load_settings
from data import ClobClient, EventFetcher, GammaClient
from data.auth import ClobAuthError
from data.rate_limits import RateLimiterRegistry
from data.rules.crypto_rules import (
    fetch_binance_btc_price,
    crypto_safety_check_live_price,
)
from data.rules.tweet_rules import (
    tweet_safety_check,
)
from db import SqliteStore
from execution import TradeExecutor
from execution.execute_trade import execute_trade
from monitoring import Dashboard, HealthState, TelegramAlerter, setup_logging
from utils.risk import RiskManager
from utils.runtime_helpers import (
    build_health_app,
    candidate_row,
    extract_token_ids,
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
    load_scheduler_intervals,
    remaining_seconds,
)


async def main() -> None:
    settings = load_settings()
    logger = setup_logging(settings.log_level)
    health = HealthState()
    dashboard = Dashboard()
    stop_event = asyncio.Event()

    discovery_poll_seconds, bucket_intervals = load_scheduler_intervals()

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

    candidate_events: dict[str, dict[str, Any]] = await load_candidate_snapshot(store, bucket_intervals)
    opportunities_queue: asyncio.PriorityQueue[tuple[float, dict[str, Any]]] = asyncio.PriorityQueue()
    circuit_breaker = CircuitBreaker(logger, alerts)

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
            store=store,
            logger=logger,
            event_filter=include_event,
            bucket_classifier=classify_event_bucket,
            bucket_matcher=bucket_time_match,
            persist_candidate_snapshot=False,
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

        async def check_event_for_99pct_and_safety(event_id: str) -> dict[str, Any] | None:
            cached = candidate_events.get(event_id)
            if not cached:
                return None

            event = await event_fetcher.refresh_event(event_id)
            if not event:
                return None

            event_bucket = str(cached.get("bucket") or classify_event_bucket(event) or "")
            if not event_bucket:
                return None

            markets = event.get("markets") or []
            if not isinstance(markets, list):
                return None

            best: dict[str, Any] | None = None
            tweet_count = event.get("tweetCount")
            title = str(event.get("title") or cached.get("title") or event_id)
            event_type_value = str(cached.get("event_type") or event_type_for_event(event) or "")

            for market in markets:
                market_id = str(market.get("id") or "")
                if not market_id:
                    continue

                token_ids = extract_token_ids(market)
                if not token_ids:
                    continue

                yes_token, no_token = token_ids
                yes_book = await clob.get_order_book_safe(yes_token, use_cache=False)
                no_book = await clob.get_order_book_safe(no_token, use_cache=False)
                if yes_book is None or no_book is None:
                    continue

                yes_ask = yes_book.best_ask()
                no_ask = no_book.best_ask()
                if max(yes_ask, no_ask) < settings.high_prob_threshold:
                    continue

                side = "YES" if yes_ask >= no_ask else "NO"
                token_id = yes_token if side == "YES" else no_token
                chosen_book = yes_book if side == "YES" else no_book
                price = yes_ask if side == "YES" else no_ask
                expected_price = float(market.get("bestAsk") or price)

                if not risk.validate_liquidity(float(market.get("liquidityNum") or market.get("liquidity") or 0.0)):
                    continue

                if not risk.slippage_ok(expected_price, price):
                    continue

                effective_slippage = abs(price - expected_price) / expected_price if expected_price > 0 else 1.0
                if effective_slippage > 0.015:
                    continue

                available = chosen_book.cumulative_notional("BUY")
                size = risk.position_size_for_price(price, available)
                if size <= 0:
                    continue

                safety_margin = 10_000.0
                if event_type_value == "tweet":
                    if not isinstance(tweet_count, int):
                        continue
                    safe, margin = tweet_safety_check(tweet_count, market)
                    if not safe:
                        continue
                    safety_margin = float(margin)
                elif event_type_value == "crypto":
                    safe, margin = await crypto_safety_check_live_price(
                        session,
                        market,
                        "1hour" if event_bucket == "hourly" else event_bucket,
                        event_title=title,
                    )
                    if not safe:
                        continue
                    safety_margin = float(margin)
                else:
                    continue

                expected_profit = max((1.0 - price) * size, 0.0)
                candidate = {
                    "event_id": event_id,
                    "bucket": event_bucket,
                    "market_id": market_id,
                    "token_id": token_id,
                    "side": side,
                    "price": price,
                    "size": size,
                    "confidence": max(yes_ask, no_ask),
                    "safety_margin": safety_margin,
                    "edge": expected_profit,
                    "strategy": "high_probability",
                    "endDate": str(event.get("endDate") or cached.get("endDate") or ""),
                }

                if best is None:
                    best = candidate
                    continue

                if candidate["edge"] > best["edge"] or (
                    candidate["edge"] == best["edge"] and candidate["safety_margin"] > best["safety_margin"]
                ):
                    best = candidate

            return best

        async def discovery_task() -> None:
            logger.info("Discovery task started (interval=%ss)", discovery_poll_seconds)
            while not stop_event.is_set():
                try:
                    if await circuit_breaker.wait_if_open():
                        continue

                    grouped = await event_fetcher.fetch_relevant_events()
                    discovered = 0
                    newly_added = 0
                    updated_existing = 0
                    current_price_btc: float | None = None

                    with contextlib.suppress(Exception):
                        current_price_btc = await fetch_binance_btc_price(session)

                    for bucket, items in grouped.items():
                        discovered += len(items)
                        for item in items:
                            item_type = event_type_for_event(item.raw_data)
                            if item_type is None:
                                continue
                            refreshed_current_price = current_price_btc if item_type == "crypto" else None
                            refreshed_tweet_count = item.tweetCount if item_type == "tweet" else None

                            if item.event_id in candidate_events:
                                # Incremental refresh for existing entries: no full event pull.
                                existing = candidate_events[item.event_id]
                                existing["title"] = item.title or existing.get("title")
                                existing["endDate"] = item.endDate or existing.get("endDate")
                                existing["bucket"] = bucket
                                existing["event_type"] = item_type
                                existing["tweetCount"] = refreshed_tweet_count
                                existing["current_price"] = refreshed_current_price
                                updated_existing += 1
                                continue

                            # New events fetch full details once, then cache.
                            full_event = await event_fetcher.refresh_event(item.event_id)
                            row_source = full_event if isinstance(full_event, dict) and full_event else item.raw_data
                            row = candidate_row(item)
                            row["bucket"] = bucket
                            row["raw_data"] = row_source
                            row["event_type"] = item_type
                            row["tweetCount"] = refreshed_tweet_count
                            row["current_price"] = refreshed_current_price
                            candidate_events[item.event_id] = row
                            newly_added += 1

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
            logger.info("Bucket task started: %s (interval=%ss)", bucket, interval_seconds)
            while not stop_event.is_set():
                try:
                    if await circuit_breaker.wait_if_open():
                        continue

                    if opportunities_queue.qsize() > 5:
                        delay = min(interval_seconds, 10)
                        logger.warning(
                            "Back-pressure active for bucket=%s queue_depth=%s; slowing polling by %ss",
                            bucket,
                            opportunities_queue.qsize(),
                            delay,
                        )
                        await asyncio.sleep(delay)
                        continue

                    bucket_event_ids = [
                        event_id
                        for event_id, event_data in candidate_events.items()
                        if str(event_data.get("bucket") or "") == bucket
                    ]

                    dashboard.scanned_markets += len(bucket_event_ids)

                    for event_id in bucket_event_ids:
                        if event_id not in candidate_events:
                            continue

                        opportunity = await check_event_for_99pct_and_safety(event_id)
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
