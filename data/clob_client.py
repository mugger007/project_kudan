from __future__ import annotations

import asyncio
import contextlib
import json
import logging
from urllib.parse import urlencode
from typing import Any, AsyncIterator

import aiohttp

from .auth import ClobAuthManager
from .cache import TTLCache
from .models import OrderBookLevel, OrderBookSnapshot
from .rate_limits import RateLimiterRegistry, clob_policy_for_path
from utils.retry import async_retry


class ClobNotFoundError(Exception):
    """Raised when a requested CLOB resource is not found (HTTP 404)."""


class ClobClient:
    """Reads market data from CLOB and prepares authenticated access for trading endpoints."""

    def __init__(
        self,
        base_url: str,
        session: aiohttp.ClientSession,
        logger: logging.Logger,
        chain_id: int,
        private_key: str,
        api_key: str = "",
        api_secret: str = "",
        api_passphrase: str = "",
        rate_limiter_registry: RateLimiterRegistry | None = None,
    ):
        """Initializes CLOB client state, auth manager, and short-lived book cache."""
        self.base_url = base_url.rstrip("/")
        self.session = session
        self.logger = logger
        self._book_cache: TTLCache[str, OrderBookSnapshot] = TTLCache(ttl_seconds=2.0)
        self._rate_limiters = rate_limiter_registry or RateLimiterRegistry()
        self._auth = ClobAuthManager(
            host=self.base_url,
            chain_id=chain_id,
            private_key=private_key,
            logger=logger,
            api_key=api_key,
            api_secret=api_secret,
            api_passphrase=api_passphrase,
        )

    async def ensure_authenticated_session(self) -> None:
        """Eagerly ensures L2 credentials exist to fail fast on auth misconfiguration."""
        await self._auth.ensure_api_credentials()

    @staticmethod
    def _request_path(path: str, params: dict[str, Any] | None) -> str:
        """Builds a deterministic request path used for L2 signature generation."""
        if not params:
            return path
        query = urlencode(params, doseq=True)
        return f"{path}?{query}" if query else path

    async def _request_json(
        self,
        method: str,
        path: str,
        params: dict[str, Any] | None = None,
        body: Any = None,
        requires_auth: bool = False,
    ) -> Any:
        """Performs a JSON HTTP call with endpoint-aware throttling and optional L2 auth."""
        policy = clob_policy_for_path(path)
        await self._rate_limiters.get(policy).acquire()

        url = f"{self.base_url}{path}"
        request_path = self._request_path(path, params)

        serialized_body = None
        if body is not None:
            serialized_body = json.dumps(body, separators=(",", ":"), sort_keys=True)

        headers: dict[str, str] = {}
        if requires_auth:
            headers.update(
                await self._auth.build_level2_headers(
                    method=method,
                    request_path=request_path,
                    body=body,
                    serialized_body=serialized_body,
                )
            )

        async def _req() -> Any:
            async with self.session.request(
                method=method,
                url=url,
                params=params,
                json=body,
                headers=headers,
                timeout=aiohttp.ClientTimeout(total=15),
            ) as resp:
                if resp.status == 404:
                    raise ClobNotFoundError(f"CLOB resource not found for path={path} params={params}")
                if resp.status == 429:
                    retry_after = resp.headers.get("Retry-After")
                    if retry_after:
                        with contextlib.suppress(ValueError):
                            await asyncio.sleep(float(retry_after))
                    raise RuntimeError("CLOB API rate-limited")
                if resp.status >= 500:
                    raise RuntimeError(f"CLOB server error status={resp.status}")
                resp.raise_for_status()
                return await resp.json()

        return await async_retry(
            _req,
            retries=3,
            base_delay=0.4,
            retry_on=(RuntimeError, aiohttp.ClientConnectionError, aiohttp.ServerTimeoutError),
        )

    async def get_order_book(self, token_id: str, use_cache: bool = True) -> OrderBookSnapshot:
        """Returns order book snapshot for a token, using cache to reduce API load."""
        if use_cache:
            cached = self._book_cache.get(token_id)
            if cached:
                return cached

        payload = await self._request_json("GET", "/book", params={"token_id": token_id}, requires_auth=False)
        snapshot = self._to_order_book(token_id, payload)
        self._book_cache.set(token_id, snapshot)
        return snapshot

    async def get_order_book_safe(self, token_id: str, use_cache: bool = True) -> OrderBookSnapshot | None:
        """Fetches order book and returns None for invalid/non-indexed token ids."""
        try:
            return await self.get_order_book(token_id, use_cache=use_cache)
        except ClobNotFoundError:
            self.logger.debug("Skipping token with no CLOB book: %s", token_id)
            return None

    async def get_open_orders(self) -> Any:
        """Example authenticated endpoint call for account-scoped CLOB data."""
        return await self._request_json("GET", "/orders", requires_auth=True)

    def _to_order_book(self, token_id: str, payload: dict[str, Any]) -> OrderBookSnapshot:
        """Normalizes raw CLOB book JSON into typed bid/ask levels."""
        bids_raw = payload.get("bids") or []
        asks_raw = payload.get("asks") or []

        def levels(rows: list[dict[str, Any]]) -> list[OrderBookLevel]:
            """Converts a raw level list into price/size objects."""
            parsed: list[OrderBookLevel] = []
            for row in rows:
                price = float(row.get("price") or 0.0)
                size = float(row.get("size") or row.get("quantity") or 0.0)
                if price > 0 and size > 0:
                    parsed.append(OrderBookLevel(price=price, size=size))
            parsed.sort(key=lambda level: level.price, reverse=True)
            return parsed

        bids = levels(bids_raw)
        asks = sorted(levels(asks_raw), key=lambda level: level.price)

        return OrderBookSnapshot(token_id=token_id, bids=bids, asks=asks)

    async def stream_quotes(self, token_ids: list[str]) -> AsyncIterator[dict[str, Any]]:
        """Streams websocket quote updates and auto-reconnects on transient disconnects."""
        # TODO: Align with latest Polymarket ws channel names if API schema changes.
        ws_url = self.base_url.replace("http", "ws") + "/ws"
        while True:
            try:
                async with self.session.ws_connect(ws_url, heartbeat=20) as ws:
                    await ws.send_str(json.dumps({"type": "subscribe", "token_ids": token_ids}))
                    async for message in ws:
                        if message.type == aiohttp.WSMsgType.TEXT:
                            yield json.loads(message.data)
                        elif message.type == aiohttp.WSMsgType.ERROR:
                            break
            except Exception as exc:
                self.logger.warning("CLOB websocket dropped: %s", exc)
                await asyncio.sleep(2)
