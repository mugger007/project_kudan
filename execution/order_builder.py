from __future__ import annotations

from dataclasses import dataclass


@dataclass(slots=True)
class OrderIntent:
    market_id: str
    token_id: str
    side: str
    price: float
    size: float
    strategy: str


def build_order(
    market_id: str,
    token_id: str,
    side: str,
    price: float,
    size: float,
    strategy: str,
) -> OrderIntent:
    """Constructs an OrderIntent with price and size rounded to 4dp — CLOB rejects more decimal places."""
    return OrderIntent(
        market_id=market_id,
        token_id=token_id,
        side=side,
        price=round(price, 4),
        size=round(size, 4),
        strategy=strategy,
    )
