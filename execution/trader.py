from __future__ import annotations

import logging
from dataclasses import dataclass

from db.sqlite_store import SqliteStore
from monitoring.alerts import TelegramAlerter

from .order_builder import OrderIntent


@dataclass(slots=True)
class TradeExecutor:
    dry_run: bool
    wallet_address: str
    private_key: str
    logger: logging.Logger
    store: SqliteStore
    alerts: TelegramAlerter

    async def execute(self, order: OrderIntent) -> str:
        if self.dry_run:
            tx_hash = f"dryrun-{order.market_id}-{order.side.lower()}"
            self.logger.info(
                "DRY RUN order strategy=%s market=%s side=%s price=%.4f size=%.4f",
                order.strategy,
                order.market_id,
                order.side,
                order.price,
                order.size,
            )
            await self.store.log_trade(
                market_id=order.market_id,
                strategy=order.strategy,
                side=order.side,
                price=order.price,
                size=order.size,
                status="dry_run",
                tx_hash=tx_hash,
            )
            await self.alerts.send(
                f"Kudan dry-run trade: {order.strategy} {order.side} {order.market_id} size={order.size}"
            )
            return tx_hash

        # TODO: Implement Polymarket CLOB signed order placement.
        # Keep this seam small so a production signer can be dropped in.
        tx_hash = "pending-real-execution"
        await self.store.log_trade(
            market_id=order.market_id,
            strategy=order.strategy,
            side=order.side,
            price=order.price,
            size=order.size,
            status="submitted",
            tx_hash=tx_hash,
        )
        return tx_hash
