from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any

import aiohttp

from data.clob_client import ClobClient
from data.event_fetcher import EventFetcher
from data.rules.crypto_rules import crypto_safety_check_live_price
from data.rules.tweet_rules import tweet_safety_check
from utils.risk import RiskManager


class HighProbabilityStrategy:
    """Evaluates one candidate event for 99% probability and safety constraints."""

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
                return None
        return None

    async def evaluate_event_opportunity(
        self,
        *,
        event_id: str,
        candidate_events: dict[str, dict[str, Any]],
        event_fetcher: EventFetcher,
        clob: ClobClient,
        risk: RiskManager,
        session: aiohttp.ClientSession,
        classify_event_bucket: Callable[[dict[str, Any]], str | None],
        event_type_for_event: Callable[[dict[str, Any]], str | None],
    ) -> dict[str, Any] | None:
        """Evaluates one candidate event and returns the best strategy-ready opportunity."""
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
                "strategy": self.name,
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
