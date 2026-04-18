from __future__ import annotations

from dataclasses import dataclass


@dataclass(slots=True)
class RiskManager:
    bankroll_usd: float
    max_bankroll_exposure_pct: float
    max_trade_exposure_pct: float
    min_liquidity_usd: float
    max_slippage_pct: float

    @property
    def max_total_exposure(self) -> float:
        return self.bankroll_usd * self.max_bankroll_exposure_pct

    @property
    def max_trade_exposure(self) -> float:
        return self.bankroll_usd * self.max_trade_exposure_pct

    def validate_liquidity(self, liquidity_usd: float) -> bool:
        return liquidity_usd >= self.min_liquidity_usd

    def position_size_for_price(self, price: float, available_liquidity: float) -> float:
        if price <= 0:
            return 0.0
        capped_notional = min(self.max_trade_exposure, available_liquidity)
        return round(capped_notional / price, 4)

    def slippage_ok(self, expected_price: float, quoted_price: float) -> bool:
        if expected_price <= 0:
            return False
        slippage = abs(quoted_price - expected_price) / expected_price
        return slippage <= self.max_slippage_pct
