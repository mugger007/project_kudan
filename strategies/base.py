from __future__ import annotations

from abc import ABC, abstractmethod

from data.models import MarketSnapshot, Opportunity


class Strategy(ABC):
    name: str

    @abstractmethod
    async def evaluate(self, market: MarketSnapshot) -> Opportunity | None:
        raise NotImplementedError
