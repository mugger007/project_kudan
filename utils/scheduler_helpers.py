from __future__ import annotations

import asyncio
import contextlib
import os
from collections import deque
from datetime import datetime, timezone
from typing import Any

from data.rules.crypto_rules import classify_crypto_bucket, crypto_bucket_time_match, is_crypto_event
from data.rules.tweet_rules import classify_tweet_bucket, is_elon_tweet_event, tweet_bucket_time_match


def required_env_int(name: str) -> int:
    value = os.getenv(name, "").strip()
    if not value:
        raise ValueError(f"Missing required environment variable: {name}")
    try:
        return int(value)
    except ValueError as exc:
        raise ValueError(f"Invalid integer value for {name}: {value}") from exc


def load_scheduler_intervals() -> tuple[int, dict[str, int]]:
    discovery_poll_seconds = required_env_int("DISCOVERY_POLL_SECONDS")
    bucket_intervals: dict[str, int] = {
        "5min": required_env_int("BUCKET_5MIN_SECONDS"),
        "15min": required_env_int("BUCKET_15MIN_SECONDS"),
        "hourly": required_env_int("BUCKET_1HOUR_SECONDS"),
        "4hour": required_env_int("BUCKET_4HOUR_SECONDS"),
        "daily": required_env_int("BUCKET_DAILY_SECONDS"),
        "weekly": required_env_int("BUCKET_WEEKLY_SECONDS"),
        "monthly": required_env_int("BUCKET_MONTHLY_SECONDS"),
    }
    return discovery_poll_seconds, bucket_intervals


def include_event(event: dict[str, Any]) -> bool:
    return is_elon_tweet_event(event) or is_crypto_event(event)


def event_type_for_event(event: dict[str, Any]) -> str | None:
    if is_elon_tweet_event(event):
        return "tweet"
    if is_crypto_event(event):
        return "crypto"
    return None


def classify_event_bucket(event: dict[str, Any]) -> str | None:
    if is_elon_tweet_event(event):
        return classify_tweet_bucket(event)
    if is_crypto_event(event):
        bucket = classify_crypto_bucket(event)
        if bucket == "1hour":
            return "hourly"
        return bucket
    return None


def bucket_time_match(event: dict[str, Any], bucket: str) -> bool:
    if is_elon_tweet_event(event):
        return tweet_bucket_time_match(event, bucket)
    if is_crypto_event(event):
        bucket_for_rule = "1hour" if bucket == "hourly" else bucket
        return crypto_bucket_time_match(event, bucket_for_rule)
    return False


def remaining_seconds(end_date: str | None) -> float:
    if not end_date:
        return 9_999_999.0
    try:
        end_dt = datetime.fromisoformat(str(end_date).replace("Z", "+00:00")).astimezone(timezone.utc)
        return max((end_dt - datetime.now(timezone.utc)).total_seconds(), 0.0)
    except Exception:
        return 9_999_999.0


class CircuitBreaker:
    def __init__(self, logger, alerts, *, threshold: int = 3, window_seconds: float = 60.0, open_seconds: float = 60.0):
        self.logger = logger
        self.alerts = alerts
        self.threshold = threshold
        self.window_seconds = window_seconds
        self.open_seconds = open_seconds
        self._failures: deque[float] = deque()
        self._open_until: float = 0.0

    @property
    def state(self) -> dict[str, Any]:
        return {
            "recent_failures": len(self._failures),
            "circuit_open_until": self._open_until,
        }

    async def record_failure(self, context: str, exc: Exception) -> None:
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
        now = asyncio.get_running_loop().time()
        if self._open_until > now:
            await asyncio.sleep(min(5.0, self._open_until - now))
            return True
        return False
