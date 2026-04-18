from __future__ import annotations

import asyncio
import contextlib
import logging
from datetime import datetime, timezone
from typing import Any

import aiohttp

from .cache import TTLCache
from .models import MarketSnapshot
from .rate_limits import RateLimiterRegistry, gamma_policy_for_path
from utils.retry import async_retry


class GammaClient:
    """Fetches and normalizes market metadata from the public Gamma API."""

    def __init__(
        self,
        base_url: str,
        session: aiohttp.ClientSession,
        logger: logging.Logger,
        rate_limiter_registry: RateLimiterRegistry | None = None,
    ):
        """Initializes Gamma client with endpoint caching and shared limiters."""
        self.base_url = base_url.rstrip("/")
        self.session = session
        self.logger = logger
        self._cache: TTLCache[str, list[dict[str, Any]]] = TTLCache(ttl_seconds=20)
        self._rate_limiters = rate_limiter_registry or RateLimiterRegistry()

    async def _get_json(self, path: str, params: dict[str, Any] | None = None) -> Any:
        """Executes GET requests with endpoint-aware client-side throttling."""
        url = f"{self.base_url}{path}"
        policy = gamma_policy_for_path(path)
        await self._rate_limiters.get(policy).acquire()

        async def _req() -> Any:
            async with self.session.get(url, params=params, timeout=aiohttp.ClientTimeout(total=20)) as resp:
                if resp.status == 429:
                    retry_after = resp.headers.get("Retry-After")
                    if retry_after:
                        with contextlib.suppress(ValueError):
                            await asyncio.sleep(float(retry_after))
                    raise RuntimeError("Gamma API rate-limited")
                resp.raise_for_status()
                return await resp.json()

        return await async_retry(_req, retries=3, base_delay=0.7)

    async def list_active_markets(self) -> list[MarketSnapshot]:
        """Returns parsed active binary markets, favoring cached snapshots when fresh."""
        cache_key = "active_markets"
        cached = self._cache.get(cache_key)
        raw_markets: list[dict[str, Any]]
        if cached is None:
            raw = await self._get_json("/markets", params={"active": "true", "closed": "false", "limit": 500})
            raw_markets = raw if isinstance(raw, list) else raw.get("data", [])
            self._cache.set(cache_key, raw_markets)
        else:
            raw_markets = cached

        snapshots: list[MarketSnapshot] = []
        for item in raw_markets:
            snapshot = self._to_snapshot(item)
            if snapshot and snapshot.is_active:
                snapshots.append(snapshot)

        self.logger.debug("Gamma active markets fetched: %s", len(snapshots))
        return snapshots

    async def fetch_events_keyset_page(
        self,
        limit: int = 100,
        after_cursor: str | None = None,
        active: bool = True,
        closed: bool = False,
        extra_params: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Fetches one paginated page from /events/keyset."""
        params: dict[str, Any] = {
            "limit": limit,
            "active": str(active).lower(),
            "closed": str(closed).lower(),
        }
        if after_cursor:
            params["after_cursor"] = after_cursor
        if extra_params:
            params.update(extra_params)
        payload = await self._get_json("/events/keyset", params=params)
        return payload if isinstance(payload, dict) else {"events": [], "next_cursor": None}

    async def fetch_event_by_id(self, event_id: str) -> dict[str, Any] | None:
        """Fetches one event with nested markets from /events/{event_id}."""
        payload = await self._get_json(f"/events/{event_id}")
        if isinstance(payload, dict):
            return payload
        return None

    def _to_snapshot(self, item: dict[str, Any]) -> MarketSnapshot | None:
        """Converts one raw Gamma market record into the app snapshot model."""
        try:
            market_id = str(item.get("id") or item.get("marketId") or "")
            slug = str(item.get("slug") or market_id)
            question = str(item.get("question") or item.get("title") or slug)

            tokens = item.get("tokens") or []
            yes_token_id = ""
            no_token_id = ""
            for token in tokens:
                outcome = str(token.get("outcome") or "").upper()
                token_id = str(token.get("token_id") or token.get("id") or "")
                if outcome == "YES":
                    yes_token_id = token_id
                elif outcome == "NO":
                    no_token_id = token_id

            yes_price = float(item.get("bestYesPrice") or item.get("yesPrice") or 0.0)
            no_price = float(item.get("bestNoPrice") or item.get("noPrice") or 0.0)
            volume_24h = float(item.get("volume24hr") or item.get("volume24h") or 0.0)
            liquidity = float(item.get("liquidity") or item.get("liquidityNum") or 0.0)

            end_iso = item.get("endDate") or item.get("endTime") or item.get("resolutionDate")
            if isinstance(end_iso, str) and end_iso:
                end_time = datetime.fromisoformat(end_iso.replace("Z", "+00:00")).astimezone(timezone.utc)
            else:
                end_time = datetime.now(timezone.utc)

            is_active = bool(item.get("active", True)) and not bool(item.get("closed", False))

            if not market_id or not yes_token_id or not no_token_id:
                return None

            return MarketSnapshot(
                market_id=market_id,
                slug=slug,
                question=question,
                yes_token_id=yes_token_id,
                no_token_id=no_token_id,
                best_yes_price=yes_price,
                best_no_price=no_price,
                volume_24h=volume_24h,
                liquidity_usd=liquidity,
                end_time=end_time,
                is_active=is_active,
            )
        except Exception:
            return None
