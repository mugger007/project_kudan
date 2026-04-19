from __future__ import annotations

import logging
import re
from typing import Any

import aiohttp

from utils.time_utils import minutes_remaining_for_event
from utils.crypto_parser import extract_market_boundary_spec, extract_market_price_boundaries
from utils.tweet_parser import min_distance_to_boundaries

logger = logging.getLogger(__name__)

BITCOIN_TAG_ID = "235"
CRYPTO_PRICES_TAG_ID = "1312"
BINANCE_BTCUSDT_TICKER_URL = "https://api.binance.com/api/v3/ticker/price"

DAILY_TITLE_PATTERN = re.compile(
    r"(?:bitcoin\s+up\s+or\s+down\s+on\s+[a-z]+\s+\d{1,2}\?|"
    r"what\s+price\s+will\s+bitcoin\s+hit\s+on\s+[a-z]+\s+\d{1,2}\?|"
    r"bitcoin\s+above\s+.+\s+on\s+[a-z]+\s+\d{1,2}\?)",
    re.IGNORECASE,
)
WEEKLY_TITLE_PATTERN = re.compile(
    r"what\s+price\s+will\s+bitcoin\s+hit\s+[a-z]+\s+\d{1,2}\s*[-–]\s*\d{1,2}\?",
    re.IGNORECASE,
)
MONTHLY_TITLE_PATTERN = re.compile(
    r"what\s+price\s+will\s+bitcoin\s+hit\s+in\s+[a-z]+\?",
    re.IGNORECASE,
)

CRYPTO_SAFETY_THRESHOLDS = {
    "5min": 0.50,
    "15min": 0.75,
    "1hour": 1.25,
    "hourly": 1.25,
    "4hour": 2.00,
    "daily": 3.00,
    "weekly": 4.00,
    "monthly": 5.00,
}


def crypto_discovery_params() -> dict[str, str]:
    """Returns a narrowed discovery query for bitcoin-tagged crypto events."""
    return {"tag_id": BITCOIN_TAG_ID}


def is_crypto_event(event: dict[str, Any]) -> bool:
    """Filters for events containing both Bitcoin and Crypto Prices tags."""
    try:
        tags = event.get("tags") or []
        tag_ids = {str(tag.get("id")) for tag in tags if isinstance(tag, dict)}
        return BITCOIN_TAG_ID in tag_ids and CRYPTO_PRICES_TAG_ID in tag_ids
    except Exception as exc:
        logger.error("is_crypto_event failed: %s", exc)
        return False


def classify_crypto_bucket(event: dict[str, Any]) -> str | None:
    """Classifies crypto events to configured buckets; returns None when unmatched."""
    try:
        slug = str(event.get("slug") or "").lower()
        title = str(event.get("title") or "").lower()

        if "btc-updown-5m" in slug:
            return "5min"
        if "btc-updown-15m" in slug:
            return "15min"
        if "btc-updown-1h" in slug:
            return "1hour"
        if "btc-updown-4h" in slug:
            return "4hour"

        if DAILY_TITLE_PATTERN.search(title):
            return "daily"
        if WEEKLY_TITLE_PATTERN.search(title):
            return "weekly"
        if MONTHLY_TITLE_PATTERN.search(title):
            return "monthly"

        return None
    except Exception as exc:
        logger.error("classify_crypto_bucket failed: %s", exc)
        return None


def crypto_bucket_time_match(event: dict[str, Any], bucket: str | None) -> bool:
    """Applies per-bucket recency windows for crypto event shortlist filtering."""
    if not bucket:
        return False

    remaining = minutes_remaining_for_event(event)
    if bucket == "5min":
        return remaining <= 1
    if bucket == "15min":
        return remaining <= 3
    if bucket in {"1hour", "hourly"}:
        return remaining <= 10
    if bucket == "4hour":
        return remaining <= 20
    if bucket == "daily":
        return remaining <= 60
    if bucket == "weekly":
        return remaining <= 12 * 60
    if bucket == "monthly":
        return remaining <= 24 * 60
    return False

def crypto_safety_check(
    current_price: float,
    market: dict[str, Any],
    bucket: str,
    event_title: str | None = None,
) -> tuple[bool, float]:
    """Checks nearest percent distance to relevant boundary type against bucket threshold."""
    if current_price <= 0:
        return False, 0.0

    market_type, boundaries = extract_market_boundary_spec(market, event_title=event_title)
    if not boundaries:
        if market_type == "updown":
            logger.error("crypto_safety_check missing up/down boundary (price-to-beat not available)")
        return False, 0.0

    if market_type == "range" and len(boundaries) >= 2:
        lo, hi = boundaries[0], boundaries[1]
        pct_distances = [abs(current_price - lo) / current_price * 100.0, abs(current_price - hi) / current_price * 100.0]
    else:
        pct_distances = [abs(current_price - boundaries[0]) / current_price * 100.0]

    # Reuse existing boundary distance helper by measuring distance from zero in basis points.
    bps_boundaries = [int(round(distance * 100)) for distance in pct_distances]
    nearest_bps = min_distance_to_boundaries(0, bps_boundaries)
    nearest_pct = nearest_bps / 100.0

    threshold = CRYPTO_SAFETY_THRESHOLDS.get(bucket)
    if threshold is None:
        return False, nearest_pct
    return nearest_pct >= threshold, nearest_pct


async def fetch_binance_btc_price(session: aiohttp.ClientSession) -> float:
    """Fetches latest BTCUSDT price from Binance public API."""
    params = {"symbol": "BTCUSDT"}
    async with session.get(BINANCE_BTCUSDT_TICKER_URL, params=params, timeout=aiohttp.ClientTimeout(total=10)) as resp:
        resp.raise_for_status()
        payload = await resp.json()
    return float(payload.get("price") or 0.0)


async def crypto_safety_check_live_price(
    session: aiohttp.ClientSession,
    market: dict[str, Any],
    bucket: str,
    event_title: str | None = None,
) -> tuple[bool, float]:
    """Runs crypto safety check using live BTCUSDT price from Binance."""
    try:
        current_price = await fetch_binance_btc_price(session)
    except Exception as exc:
        logger.error("fetch_binance_btc_price failed: %s", exc)
        return False, 0.0

    return crypto_safety_check(current_price=current_price, market=market, bucket=bucket, event_title=event_title)
