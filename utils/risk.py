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
        """Max USD value allowed across all open positions simultaneously."""
        return self.bankroll_usd * self.max_bankroll_exposure_pct

    @property
    def max_trade_exposure(self) -> float:
        """Max USD notional for a single trade."""
        return self.bankroll_usd * self.max_trade_exposure_pct

    def validate_liquidity(self, liquidity_usd: float) -> bool:
        """Rejects markets below the minimum liquidity threshold — thin books cause excessive slippage."""
        return liquidity_usd >= self.min_liquidity_usd

    def position_size_for_price(self, price: float, available_liquidity: float) -> float:
        """Converts USD notional to token quantity, capped at available book depth to avoid overfilling."""
        if price <= 0:
            return 0.0
        capped_notional = min(self.max_trade_exposure, available_liquidity)
        return round(capped_notional / price, 4)

    def slippage_ok(self, expected_price: float, quoted_price: float) -> bool:
        """Guards against stale cached prices; rejects if quoted deviates from expected by more than max_slippage_pct."""
        if expected_price <= 0:
            return False
        slippage = abs(quoted_price - expected_price) / expected_price
        return slippage <= self.max_slippage_pct
