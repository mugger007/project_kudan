from __future__ import annotations

from datetime import datetime, timezone
from typing import Any


def parse_iso_utc(value: str | None) -> datetime | None:
    """Parses an ISO datetime string and normalizes to UTC when possible."""
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(timezone.utc)
    except ValueError:
        return None


def minutes_remaining_for_event(event: dict[str, Any]) -> float:
    """Computes minutes to event endDate; large default when unavailable."""
    end_ts = parse_iso_utc(str(event.get("endDate") or ""))
    if end_ts is None:
        return 10_000.0
    return max((end_ts - datetime.now(timezone.utc)).total_seconds() / 60.0, 0.0)
