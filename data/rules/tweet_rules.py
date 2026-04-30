from __future__ import annotations

import logging
import re
from typing import Any

from utils.time_utils import minutes_remaining_for_event
from utils.tweet_parser import extract_boundaries, min_distance_to_boundaries

logger = logging.getLogger(__name__)

TWEET_TAG_ID = "972"

TWEET_DAILY_RANGE_PATTERN = re.compile(
    r"#\s*tweets?\s+[a-z]+\s+\d{1,2}\s*-\s*[a-z]+\s+\d{1,2},\s*\d{4}\??",
    re.IGNORECASE,
)
TWEET_MONTHLY_SPAN_PATTERN = re.compile(
    r"#\s*tweets?\s+in\s+[a-z]+\s+\d{4}\??",
    re.IGNORECASE,
)


def is_elon_tweet_event(event: dict[str, Any]) -> bool:
    """Checks whether an event belongs to the Elon tweet event series."""
    try:
        title = str(event.get("title") or "").lower()
        ticker = str(event.get("ticker") or "").lower()
        tags = event.get("tags") or []
        tag_ids = {str(tag.get("id")) for tag in tags if isinstance(tag, dict)}
        return (
            "elon-musk-of-tweets" in ticker or "elon musk # tweets" in title
        ) and TWEET_TAG_ID in tag_ids
    except Exception as exc:
        logger.error("is_elon_tweet_event failed: %s", exc)
        return False


def classify_tweet_bucket(event: dict[str, Any]) -> str:
    """Classifies tweet events into daily/weekly/monthly buckets only."""
    try:
        title = str(event.get("title") or "").lower()

        if TWEET_DAILY_RANGE_PATTERN.search(title):
            return "daily"
        if TWEET_MONTHLY_SPAN_PATTERN.search(title):
            return "monthly"

    except Exception as exc:
        logger.error("classify_tweet_bucket failed: %s", exc)
        return False


def tweet_bucket_time_match(event: dict[str, Any], bucket: str) -> bool:
    """Applies tweet-workflow timing rules for shortlist prefiltering."""
    remaining = minutes_remaining_for_event(event)
    if bucket == "daily":
        return remaining <= 60
    if bucket == "weekly":
        return remaining <= 12 * 60
    if bucket == "monthly":
        return remaining <= 24 * 60
    return False


def tweet_safety_check(
    tweet_count: int, market: dict[str, Any], boundary_tolerance: int = 10
) -> tuple[bool, int]:
    """Rejects tweet range markets too close to boundaries and returns safety margin."""
    group_title = str(market.get("groupItemTitle") or market.get("question") or "")
    boundaries = extract_boundaries(group_title)
    nearest = min_distance_to_boundaries(tweet_count, boundaries)
    return nearest > boundary_tolerance, nearest
