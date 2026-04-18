from __future__ import annotations

from dataclasses import dataclass


@dataclass(slots=True)
class Dashboard:
    scanned_markets: int = 0
    opportunities_found: int = 0
    trades_sent: int = 0

    def as_line(self) -> str:
        return (
            f"[Kudan] scanned={self.scanned_markets} "
            f"opps={self.opportunities_found} trades={self.trades_sent}"
        )
