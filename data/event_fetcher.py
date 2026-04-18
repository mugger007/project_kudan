from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from db.sqlite_store import SqliteStore
from utils.tweet_parser import extract_boundaries, min_distance_to_boundaries

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


def _parse_iso(value: str | None) -> datetime | None:
    """Parses an ISO datetime string to UTC when present."""
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(timezone.utc)
    except ValueError:
        return None


def _minutes_remaining(event: dict[str, Any]) -> float:
    """Computes time-to-resolution for event bucketing and prefiltering."""
    end_ts = _parse_iso(str(event.get("endDate") or ""))
    if end_ts is None:
        return 10_000.0
    return max((end_ts - datetime.now(timezone.utc)).total_seconds() / 60.0, 0.0)


def classify_bucket(event: dict) -> str:
    """Classifies events into 5min/hourly/daily/weekly/monthly buckets."""
    title = str(event.get("title") or "").lower()
    remaining_minutes = _minutes_remaining(event)

    if "5 min" in title or remaining_minutes <= 10:
        return "5min"
    if any(term in title for term in ["hour", "next hour"]) or 10 < remaining_minutes <= 90:
        return "hourly"
    if any(term in title for term in ["daily", "april", "may", "june"]) or 90 < remaining_minutes <= 36 * 60:
        return "daily"

    remaining_hours = remaining_minutes / 60.0
    if "week" in title or 36 < remaining_hours <= 8 * 24:
        return "weekly"

    return "monthly"


def _bucket_time_match(bucket: str, remaining_minutes: float) -> bool:
    """Applies strict prefilter rules to keep only timing-consistent candidates."""
    if bucket == "5min":
        return remaining_minutes <= 10
    if bucket == "hourly":
        return 10 < remaining_minutes <= 90
    if bucket == "daily":
        # Requested stricter daily rule: keep only if less than 60 minutes left.
        return remaining_minutes <= 60
    if bucket == "weekly":
        return 36 * 60 < remaining_minutes <= 8 * 24 * 60
    return remaining_minutes > 8 * 24 * 60


def tweet_safety_check(tweet_count: int, market: dict[str, Any], boundary_tolerance: int = 10) -> tuple[bool, int]:
    """Rejects tweet range markets too close to boundaries and returns safety margin."""
    group_title = str(market.get("groupItemTitle") or market.get("question") or "")
    boundaries = extract_boundaries(group_title)
    nearest = min_distance_to_boundaries(tweet_count, boundaries)
    return nearest > boundary_tolerance, nearest


class EventFetcher:
    """Discovers, classifies, and caches high-probability candidate events."""

    def __init__(self, gamma: GammaClient, store: SqliteStore, logger):
        self.gamma = gamma
        self.store = store
        self.logger = logger

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

    @staticmethod
    def _is_tweet_event(event: dict[str, Any]) -> bool:
        """Checks tweet market relevance by tag and title/ticker terms."""
        title = str(event.get("title") or "").lower()
        ticker = str(event.get("ticker") or "").lower()

        if "elon-musk-of-tweets" not in ticker and "elon musk # tweets" not in title:
            return False

        tags = event.get("tags") or []
        tag_ids = {str(tag.get("id")) for tag in tags if isinstance(tag, dict)}
        return "972" in tag_ids

    async def fetch_relevant_events(self) -> dict[str, list[CandidateEvent]]:
        """Runs discovery + classification + shortlist caching and returns grouped candidates."""
        tweet_events = await self._fetch_keyset_all(extra_params={"tag_id": "972"})

        seen: set[str] = set()
        shortlisted: list[CandidateEvent] = []

        for event in tweet_events:
            if not self._is_tweet_event(event):
                continue
            event_id = str(event.get("id") or "")
            if not event_id or event_id in seen:
                continue
            seen.add(event_id)
            bucket = classify_bucket(event)
            remaining = _minutes_remaining(event)
            if not _bucket_time_match(bucket, remaining):
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

        grouped: dict[str, list[CandidateEvent]] = {"5min": [], "hourly": [], "daily": [], "weekly": [], "monthly": []}
        for candidate in shortlisted:
            grouped[candidate.bucket].append(candidate)

        self.logger.info(
            "Discovery cycle complete: 5min=%s hourly=%s daily=%s weekly=%s monthly=%s",
            len(grouped["5min"]),
            len(grouped["hourly"]),
            len(grouped["daily"]),
            len(grouped["weekly"]),
            len(grouped["monthly"]),
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
