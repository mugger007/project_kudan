from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone


@dataclass(slots=True)
class HealthState:
    last_market_scan_ts: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    last_trade_ts: datetime | None = None
    rpc_ok: bool = True
    api_ok: bool = True

    def heartbeat(self) -> None:
        self.last_market_scan_ts = datetime.now(timezone.utc)
