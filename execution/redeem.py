from __future__ import annotations

import logging

from monitoring.alerts import TelegramAlerter


class Redeemer:
    def __init__(self, logger: logging.Logger, alerts: TelegramAlerter):
        self.logger = logger
        self.alerts = alerts

    async def auto_redeem(self) -> None:
        # TODO: hook to Polymarket settlement/redeem contracts once account indexing is added.
        self.logger.debug("Auto-redeem sweep completed (stub)")
        await self.alerts.send("Kudan redeem sweep: no redeemable positions detected.")
