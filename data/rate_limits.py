from __future__ import annotations

import asyncio
from collections import deque
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class RateLimitPolicy:
    """Represents a single API rate-limit window."""

    name: str
    max_requests: int
    period_seconds: float


# Polymarket Gamma API limits from docs (req / 10s).
GAMMA_GENERAL_POLICY = RateLimitPolicy("gamma_general", 4000, 10.0)
GAMMA_MARKETS_POLICY = RateLimitPolicy("gamma_markets", 300, 10.0)
GAMMA_EVENTS_POLICY = RateLimitPolicy("gamma_events", 500, 10.0)

# Polymarket CLOB API limits from docs (req / 10s).
CLOB_GENERAL_POLICY = RateLimitPolicy("clob_general", 9000, 10.0)
CLOB_BOOK_POLICY = RateLimitPolicy("clob_book", 1500, 10.0)
CLOB_BOOKS_POLICY = RateLimitPolicy("clob_books", 500, 10.0)
CLOB_AUTH_POLICY = RateLimitPolicy("clob_auth_endpoints", 100, 10.0)


def gamma_policy_for_path(path: str) -> RateLimitPolicy:
    """Returns the most specific Gamma limit policy for a request path."""
    if path.startswith("/markets"):
        return GAMMA_MARKETS_POLICY
    if path.startswith("/events"):
        return GAMMA_EVENTS_POLICY
    return GAMMA_GENERAL_POLICY


def clob_policy_for_path(path: str) -> RateLimitPolicy:
    """Returns the most specific CLOB limit policy for a request path."""
    if path.startswith("/book"):
        return CLOB_BOOK_POLICY
    if path.startswith("/books"):
        return CLOB_BOOKS_POLICY
    if path.startswith("/auth/"):
        return CLOB_AUTH_POLICY
    return CLOB_GENERAL_POLICY


class SlidingWindowRateLimiter:
    """Applies client-side throttling using a sliding window."""

    def __init__(self, policy: RateLimitPolicy):
        self.policy = policy
        self._timestamps: deque[float] = deque()
        self._lock = asyncio.Lock()

    async def acquire(self) -> None:
        """Waits until the next request can be made under the configured policy."""
        while True:
            async with self._lock:
                now = asyncio.get_running_loop().time()
                cutoff = now - self.policy.period_seconds

                while self._timestamps and self._timestamps[0] <= cutoff:
                    self._timestamps.popleft()

                if len(self._timestamps) < self.policy.max_requests:
                    self._timestamps.append(now)
                    return

                wait_for = max(self._timestamps[0] + self.policy.period_seconds - now, 0.01)

            await asyncio.sleep(wait_for)


class RateLimiterRegistry:
    """Stores one limiter instance per policy name for reuse across requests."""

    def __init__(self):
        self._limiters: dict[str, SlidingWindowRateLimiter] = {}

    def get(self, policy: RateLimitPolicy) -> SlidingWindowRateLimiter:
        """Returns an existing limiter for a policy or creates one lazily."""
        limiter = self._limiters.get(policy.name)
        if limiter is None:
            limiter = SlidingWindowRateLimiter(policy)
            self._limiters[policy.name] = limiter
        return limiter