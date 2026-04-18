from __future__ import annotations

from execution.order_builder import build_order
from execution.trader import TradeExecutor


async def execute_trade(
    *,
    event_id: str,
    market_id: str,
    token_id: str,
    side: str,
    price: float,
    size: float,
    strategy: str,
    trader: TradeExecutor,
) -> str:
    """Builds and submits one order for a chosen high-probability candidate market."""
    order = build_order(
        market_id=market_id,
        token_id=token_id,
        side=f"BUY_{side}",
        price=price,
        size=size,
        strategy=strategy,
    )
    return await trader.execute(order)
