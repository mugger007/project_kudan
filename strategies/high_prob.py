from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from data.clob_client import ClobClient
from data.event_fetcher import CandidateEvent, EventFetcher
from data.rules.tweet_rules import tweet_safety_check
from db.sqlite_store import SqliteStore
from execution.execute_trade import execute_trade
from execution.trader import TradeExecutor
from utils.risk import RiskManager


@dataclass(slots=True)
class BestMarket:
    """Represents the single highest-quality market selected for execution."""

    event_id: str
    market_id: str
    side: str
    confidence: float
    safety_margin: float
    expected_profit: float
    token_id: str
    price: float
    size: float
    strategy: str = "high_probability"


class HighProbabilityStrategy:
    """Implements tweet-focused event scanning and single-best-market selection."""

    name = "high_probability"

    def __init__(self, probability_threshold: float):
        """Initializes strategy thresholds for high-probability entry checks."""
        self.probability_threshold = probability_threshold

    @staticmethod
    def _extract_token_ids(market: dict[str, Any]) -> tuple[str, str] | None:
        """Parses binary YES/NO token ids from clobTokenIds payload."""
        raw = market.get("clobTokenIds")
        if isinstance(raw, list) and len(raw) >= 2:
            return str(raw[0]), str(raw[1])
        if isinstance(raw, str) and raw:
            try:
                parsed = json.loads(raw)
                if isinstance(parsed, list) and len(parsed) >= 2:
                    return str(parsed[0]), str(parsed[1])
            except json.JSONDecodeError:
                pass
        return None

    async def scan_high_prob_candidates(
        self,
        *,
        bucket: str,
        event_fetcher: EventFetcher,
        clob: ClobClient,
        risk: RiskManager,
        store: SqliteStore,
        trader: TradeExecutor,
        logger,
    ) -> BestMarket | None:
        """Scans one bucket, returns best candidate, and executes it when valid."""
        candidates = await event_fetcher.list_bucket_candidates(bucket)
        best: BestMarket | None = None

        for cached in candidates:
            event = await event_fetcher.refresh_event(cached.event_id)
            if not event:
                continue

            await store.log_scan(
                market_id=str(event.get("id") or cached.event_id),
                strategy=f"{self.name}:{bucket}",
                payload={
                    "title": event.get("title"),
                    "tweetCount": event.get("tweetCount"),
                    "market_count": len(event.get("markets") or []),
                },
            )

            tweet_count = event.get("tweetCount")
            markets = event.get("markets") or []
            event_best: BestMarket | None = None

            for market in markets:
                market_id = str(market.get("id") or "")
                if not market_id:
                    continue

                token_ids = self._extract_token_ids(market)
                if not token_ids:
                    continue

                yes_token, no_token = token_ids
                yes_book = await clob.get_order_book_safe(yes_token, use_cache=False)
                no_book = await clob.get_order_book_safe(no_token, use_cache=False)
                if yes_book is None or no_book is None:
                    continue
                yes_ask = yes_book.best_ask()
                no_ask = no_book.best_ask()

                if max(yes_ask, no_ask) < self.probability_threshold:
                    continue

                side = "YES" if yes_ask >= no_ask else "NO"
                token_id = yes_token if side == "YES" else no_token
                price = yes_ask if side == "YES" else no_ask
                expected_price = float(market.get("bestAsk") or price)

                if not risk.validate_liquidity(float(market.get("liquidityNum") or market.get("liquidity") or 0.0)):
                    continue

                if not risk.slippage_ok(expected_price, price):
                    continue

                effective_slippage = abs(price - expected_price) / expected_price if expected_price > 0 else 1.0
                if effective_slippage > 0.015:
                    # Keep explicit hard cap requested by workflow (<1.5%).
                    continue

                available = (yes_book.cumulative_notional("BUY") if side == "YES" else no_book.cumulative_notional("BUY"))
                size = risk.position_size_for_price(price, available)
                if size <= 0:
                    continue

                safety_margin = 10_000.0
                if isinstance(tweet_count, int):
                    safe, margin = tweet_safety_check(tweet_count, market)
                    if not safe:
                        continue
                    safety_margin = float(margin)

                expected_profit = max((1.0 - price) * size, 0.0)
                candidate = BestMarket(
                    event_id=str(event.get("id") or cached.event_id),
                    market_id=market_id,
                    side=side,
                    confidence=0.99,
                    safety_margin=safety_margin,
                    expected_profit=expected_profit,
                    token_id=token_id,
                    price=price,
                    size=size,
                )

                if event_best is None:
                    event_best = candidate
                    continue

                if isinstance(tweet_count, int):
                    if candidate.safety_margin > event_best.safety_margin:
                        event_best = candidate
                else:
                    if candidate.confidence > event_best.confidence:
                        event_best = candidate

            if event_best is None:
                continue

            if best is None or event_best.expected_profit > best.expected_profit:
                best = event_best

        if best is None:
            return None

        await store.log_opportunity(
            market_id=best.market_id,
            strategy=self.name,
            side=best.side,
            edge=best.expected_profit,
            confidence=best.confidence,
            metadata={
                "event_id": best.event_id,
                "bucket": bucket,
                "safety_margin": best.safety_margin,
                "price": best.price,
                "size": best.size,
            },
        )

        if trader.dry_run:
            logger.info(
                "DRY RUN best candidate event=%s market=%s side=%s confidence=%.2f margin=%.2f expected_profit=%.4f",
                best.event_id,
                best.market_id,
                best.side,
                best.confidence,
                best.safety_margin,
                best.expected_profit,
            )
            return best

        await execute_trade(
            event_id=best.event_id,
            market_id=best.market_id,
            token_id=best.token_id,
            side=best.side,
            price=best.price,
            size=best.size,
            strategy=self.name,
            trader=trader,
        )
        return best
