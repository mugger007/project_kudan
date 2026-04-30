from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Generic, TypeVar

K = TypeVar("K")
V = TypeVar("V")


@dataclass(slots=True)
class CacheItem(Generic[V]):
    value: V
    expires_at: float


class TTLCache(Generic[K, V]):
    """In-process key-value cache with lazy expiry — expired entries are evicted on read, not on a background timer."""

    def __init__(self, ttl_seconds: float):
        self._ttl_seconds = ttl_seconds
        self._store: dict[K, CacheItem[V]] = {}

    def get(self, key: K) -> V | None:
        """Returns None for missing or expired keys; deletes the entry on expiry."""
        item = self._store.get(key)
        if not item:
            return None
        if item.expires_at <= time.time():
            del self._store[key]
            return None
        return item.value

    def set(self, key: K, value: V) -> None:
        """Inserts or overwrites the entry, resetting the TTL clock."""
        self._store[key] = CacheItem(value=value, expires_at=time.time() + self._ttl_seconds)

    def clear(self) -> None:
        """Removes all entries regardless of expiry state."""
        self._store.clear()
