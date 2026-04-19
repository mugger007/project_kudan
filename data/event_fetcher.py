from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .gamma_client import GammaClient
from utils.time_utils import is_event_not_expired


@dataclass(slots=True)
class CandidateEvent:
    """Normalized shortlisted event used by bucket scanners."""

    event_id: str
    title: str
    endDate: str
    tweetCount: int | None
    event_type: str
    current_price: float | None
    bucket: str
    raw_data: dict[str, Any]


class EventFetcher:
    """Fetches raw event data and refreshed event details from Gamma."""

    def __init__(self, gamma: GammaClient):
        """Initializes Gamma discovery client state."""
        self.gamma = gamma

    async def fetch_events(self) -> list[dict[str, Any]]:
        """Walks /events/keyset cursor pagination and returns active discovery rows."""
        events: list[dict[str, Any]] = []
        cursor: str | None = None

        while True:
            page = await self.gamma.fetch_events_keyset_page(
                limit=100,
                after_cursor=cursor,
                active=True,
                closed=False,
                extra_params=None,
            )
            batch = page.get("events") or []
            events.extend(
                [
                    event
                    for event in batch
                    if isinstance(event, dict) and is_event_not_expired(event)
                ]
            )
            cursor = page.get("next_cursor")
            if not cursor:
                break

        return events

    async def refresh_event(self, event_id: str) -> dict[str, Any] | None:
        """Refreshes one candidate event for bucket checks and market pricing decisions."""
        return await self.gamma.fetch_event_by_id(event_id)
