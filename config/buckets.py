from __future__ import annotations

from enum import Enum


class TimeBucket(str, Enum):
    ULTRA_SHORT = "ultra_short"
    SHORT = "short"
    MEDIUM = "medium"


def bucket_for_seconds(seconds_remaining: float) -> TimeBucket:
    if seconds_remaining <= 5 * 60:
        return TimeBucket.ULTRA_SHORT
    if seconds_remaining <= 60 * 60:
        return TimeBucket.SHORT
    return TimeBucket.MEDIUM


def iter_bucket_order() -> list[TimeBucket]:
    # Highest urgency first.
    return [TimeBucket.ULTRA_SHORT, TimeBucket.SHORT, TimeBucket.MEDIUM]
