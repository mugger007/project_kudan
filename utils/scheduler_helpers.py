from __future__ import annotations

import asyncio
import contextlib
from collections import deque
from datetime import datetime, timezone
from typing import Any

from data.rules.crypto_rules import classify_crypto_bucket, crypto_bucket_time_match, is_crypto_event
from data.rules.tweet_rules import classify_tweet_bucket, is_elon_tweet_event, tweet_bucket_time_match


def include_event(event: dict[str, Any]) -> bool:
    """Returns True for supported tweet or crypto events."""
    return is_elon_tweet_event(event) or is_crypto_event(event)


def event_type_for_event(event: dict[str, Any]) -> str | None:
    """Classifies event type using explicit rule modules only."""
    if is_elon_tweet_event(event):
        return "tweet"
    if is_crypto_event(event):
        return "crypto"
    return None


def classify_event_bucket(event: dict[str, Any]) -> str | None:
    """Maps an event to a polling bucket using rule-specific classifiers."""
    if is_elon_tweet_event(event):
        return classify_tweet_bucket(event)
    if is_crypto_event(event):
        bucket = classify_crypto_bucket(event)
        return bucket
    return None


def bucket_time_match(event: dict[str, Any], bucket: str) -> bool:
    """Checks whether event timing fits the requested bucket window."""
    if is_elon_tweet_event(event):
        return tweet_bucket_time_match(event, bucket)
    if is_crypto_event(event):
        return crypto_bucket_time_match(event, bucket)
    return False


def remaining_seconds(end_date: str | None) -> float:
    """Returns seconds until end date or a large sentinel when unavailable."""
    if not end_date:
        return 9_999_999.0
    try:
        end_dt = datetime.fromisoformat(str(end_date).replace("Z", "+00:00")).astimezone(timezone.utc)
        return max((end_dt - datetime.now(timezone.utc)).total_seconds(), 0.0)
    except Exception:
        return 9_999_999.0


class CircuitBreaker:
    """Tracks recent failures and pauses polling after threshold breaches."""

    def __init__(self, logger, alerts, *, threshold: int = 3, window_seconds: float = 60.0, open_seconds: float = 60.0):
        """Initializes circuit breaker thresholds and state."""
        self.logger = logger
        self.alerts = alerts
        self.threshold = threshold
        self.window_seconds = window_seconds
        self.open_seconds = open_seconds
        self._failures: deque[float] = deque()
        self._open_until: float = 0.0

    @property
    def state(self) -> dict[str, Any]:
        """Returns health-compatible breaker status snapshot."""
        return {
            "recent_failures": len(self._failures),
            "circuit_open_until": self._open_until,
        }

    async def record_failure(self, context: str, exc: Exception) -> None:
        """Records a failure and opens breaker with alert when threshold is exceeded."""
        loop = asyncio.get_running_loop()
        now = loop.time()
        self._failures.append(now)
        while self._failures and now - self._failures[0] > self.window_seconds:
            self._failures.popleft()

        if len(self._failures) > self.threshold and now >= self._open_until:
            self._open_until = now + self.open_seconds
            alert_message = (
                "Circuit breaker opened: more than 3 failures detected within 60 seconds. "
                "Scheduler polling paused for 60s."
            )
            self.logger.error("%s Last context=%s error=%s", alert_message, context, exc)
            with contextlib.suppress(Exception):
                await self.alerts.send(alert_message)

    async def wait_if_open(self) -> bool:
        """Sleeps briefly while breaker is open; returns True when pause was applied."""
        now = asyncio.get_running_loop().time()
        if self._open_until > now:
            await asyncio.sleep(min(5.0, self._open_until - now))
            return True
        return False
