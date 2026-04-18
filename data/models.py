from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any


@dataclass(slots=True)
class MarketSnapshot:
    market_id: str
    slug: str
    question: str
    yes_token_id: str
    no_token_id: str
    best_yes_price: float
    best_no_price: float
    volume_24h: float
    liquidity_usd: float
    end_time: datetime
    is_active: bool

    @property
    def seconds_to_resolution(self) -> float:
        return max((self.end_time - datetime.now(timezone.utc)).total_seconds(), 0.0)

    @property
    def implied_favorite_probability(self) -> float:
        return max(self.best_yes_price, self.best_no_price)

    @property
    def favorite_side(self) -> str:
        return "YES" if self.best_yes_price >= self.best_no_price else "NO"


@dataclass(slots=True)
class OrderBookLevel:
    price: float
    size: float


@dataclass(slots=True)
class OrderBookSnapshot:
    token_id: str
    bids: list[OrderBookLevel]
    asks: list[OrderBookLevel]

    def best_bid(self) -> float:
        return self.bids[0].price if self.bids else 0.0

    def best_ask(self) -> float:
        return self.asks[0].price if self.asks else 1.0

    def cumulative_notional(self, side: str, limit_levels: int = 10) -> float:
        levels = self.asks if side.upper() == "BUY" else self.bids
        return sum(level.price * level.size for level in levels[:limit_levels])


@dataclass(slots=True)
class Opportunity:
    strategy: str
    market_id: str
    side: str
    edge: float
    confidence: float
    metadata: dict[str, Any] = field(default_factory=dict)
