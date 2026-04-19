from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from db.sqlite_store import SqliteStore

from .gamma_client import GammaClient


@dataclass(slots=True)
class CandidateEvent:
    """Normalized shortlisted event used by bucket scanners."""

    event_id: str
    title: str
    endDate: str
    tweetCount: int | None
    bucket: str
    raw_data: dict[str, Any]


class EventFetcher:
    """Discovers, classifies, and caches high-probability candidate events."""

    def __init__(
        self,
        gamma: GammaClient,
        store: SqliteStore,
        logger,
        *,
        discovery_params: dict[str, Any] | None = None,
        event_filter: Callable[[dict[str, Any]], bool] | None = None,
        bucket_classifier: Callable[[dict[str, Any]], str | None] | None = None,
        bucket_matcher: Callable[[dict[str, Any], str], bool] | None = None,
        persist_candidate_snapshot: bool = True,
    ):
        self.gamma = gamma
        self.store = store
        self.logger = logger
        self.discovery_params = discovery_params
        self.event_filter = event_filter
        self.bucket_classifier = bucket_classifier
        self.bucket_matcher = bucket_matcher
        self.persist_candidate_snapshot = persist_candidate_snapshot

    async def _fetch_keyset_all(self, extra_params: dict[str, Any] | None = None) -> list[dict[str, Any]]:
        """Walks /events/keyset cursor pagination and returns active event rows."""
        events: list[dict[str, Any]] = []
        cursor: str | None = None

        while True:
            page = await self.gamma.fetch_events_keyset_page(
                limit=100,
                after_cursor=cursor,
                active=True,
                closed=False,
                extra_params=extra_params,
            )
            batch = page.get("events") or []
            events.extend([event for event in batch if isinstance(event, dict)])
            cursor = page.get("next_cursor")
            if not cursor:
                break

        return events

    async def fetch_relevant_events(self) -> dict[str, list[CandidateEvent]]:
        """Runs discovery + classification + shortlist caching and returns grouped candidates."""
        if self.bucket_classifier is None or self.bucket_matcher is None:
            raise ValueError("EventFetcher requires bucket_classifier and bucket_matcher")

        events = await self._fetch_keyset_all(extra_params=self.discovery_params)

        seen: set[str] = set()
        filtered_events: list[dict[str, str]] = []
        shortlisted: list[CandidateEvent] = []

        for event in events:
            if self.event_filter is not None and not self.event_filter(event):
                continue
            event_id = str(event.get("id") or "")
            if not event_id or event_id in seen:
                continue
            seen.add(event_id)
            bucket = self.bucket_classifier(event)
            if not bucket:
                # Rule modules may return None/empty for unmatched events.
                continue
            filtered_events.append(
                {
                    "event_id": event_id,
                    "title": str(event.get("title") or event_id),
                    "classification": bucket,
                }
            )
            if not self.bucket_matcher(event, bucket):
                continue
            shortlisted.append(
                CandidateEvent(
                    event_id=event_id,
                    title=str(event.get("title") or event_id),
                    endDate=str(event.get("endDate") or ""),
                    tweetCount=event.get("tweetCount"),
                    bucket=bucket,
                    raw_data=event,
                )
            )

        await self.store.replace_filtered_events(filtered_events)

        if self.persist_candidate_snapshot:
            await self.store.replace_candidate_events(
                [
                    {
                        "event_id": item.event_id,
                        "title": item.title,
                        "endDate": item.endDate,
                        "tweetCount": item.tweetCount,
                        "bucket": item.bucket,
                        "raw_data": item.raw_data,
                    }
                    for item in shortlisted
                ]
            )

        grouped: dict[str, list[CandidateEvent]] = {}
        for candidate in shortlisted:
            grouped.setdefault(candidate.bucket, []).append(candidate)

        self.logger.info(
            "Discovery cycle complete: %s",
            ", ".join(f"{name}={len(items)}" for name, items in sorted(grouped.items())) or "none",
        )
        return grouped

    async def list_bucket_candidates(self, bucket: str) -> list[CandidateEvent]:
        """Reads latest cached shortlist for one bucket from SQLite."""
        rows = await self.store.list_candidate_events(bucket)
        return [
            CandidateEvent(
                event_id=str(row.get("event_id") or ""),
                title=str(row.get("title") or ""),
                endDate=str(row.get("endDate") or ""),
                tweetCount=row.get("tweetCount"),
                bucket=str(row.get("bucket") or "monthly"),
                raw_data=row.get("raw_data") or {},
            )
            for row in rows
            if row.get("event_id")
        ]

    async def refresh_event(self, event_id: str) -> dict[str, Any] | None:
        """Refreshes one candidate event for bucket checks and market pricing decisions."""
        return await self.gamma.fetch_event_by_id(event_id)
