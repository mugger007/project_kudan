from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Sequence

from dotenv import load_dotenv


@dataclass(slots=True)
class AppSettings:
    """Container for all runtime configuration loaded from environment variables."""

    polymarket_private_key: str
    polymarket_wallet_address: str
    polymarket_chain_id: int

    clob_api_key: str
    clob_api_secret: str
    clob_api_passphrase: str

    telegram_bot_token: str
    telegram_chat_id: str

    gamma_base_url: str
    clob_base_url: str

    polygon_rpc_primary: str
    polygon_rpc_fallbacks: list[str]

    db_path: str
    log_level: str
    dry_run: bool

    max_bankroll_exposure_pct: float
    max_trade_exposure_pct: float
    min_liquidity_usd: float
    max_slippage_pct: float
    high_prob_threshold: float

    vpn_enabled: bool
    vpn_reconnect_seconds: int
    openvpn_config_file: str
    openvpn_executable: str
    openvpn_auth_file: str


def _parse_bool(value: str | None, default: bool = False) -> bool:
    """Parses a loose boolean string while supporting common truthy values."""
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _parse_csv(value: str | None) -> list[str]:
    """Parses comma-separated strings into a trimmed list without empty values."""
    if not value:
        return []
    return [item.strip() for item in value.split(",") if item.strip()]


def _required(name: str) -> str:
    """Returns a required env var and raises a clear error when missing."""
    value = os.getenv(name, "").strip()
    if not value:
        raise ValueError(f"Missing required environment variable: {name}")
    return value


def load_settings(extra_env_files: Sequence[str] | None = None) -> AppSettings:
    """Loads and validates runtime settings from .env and process environment."""
    load_dotenv()
    for path in extra_env_files or []:
        load_dotenv(path, override=False)

    return AppSettings(
        polymarket_private_key=_required("POLYMARKET_PRIVATE_KEY"),
        polymarket_wallet_address=_required("POLYMARKET_WALLET_ADDRESS"),
        polymarket_chain_id=int(os.getenv("POLYMARKET_CHAIN_ID", "137")),
        clob_api_key=os.getenv("POLY_CLOB_API_KEY", "").strip(),
        clob_api_secret=os.getenv("POLY_CLOB_API_SECRET", "").strip(),
        clob_api_passphrase=os.getenv("POLY_CLOB_API_PASSPHRASE", "").strip(),
        telegram_bot_token=os.getenv("TELEGRAM_BOT_TOKEN", "").strip(),
        telegram_chat_id=os.getenv("TELEGRAM_CHAT_ID", "").strip(),
        gamma_base_url=os.getenv("GAMMA_BASE_URL", "https://gamma-api.polymarket.com").strip(),
        clob_base_url=os.getenv("CLOB_BASE_URL", "https://clob.polymarket.com").strip(),
        polygon_rpc_primary=_required("POLYGON_RPC_PRIMARY"),
        polygon_rpc_fallbacks=_parse_csv(os.getenv("POLYGON_RPC_FALLBACKS")),
        db_path=os.getenv("DB_PATH", "./kudan.db").strip(),
        log_level=os.getenv("LOG_LEVEL", "INFO").strip(),
        dry_run=_parse_bool(os.getenv("DRY_RUN"), default=True),
        max_bankroll_exposure_pct=float(os.getenv("MAX_BANKROLL_EXPOSURE_PCT", "0.05")),
        max_trade_exposure_pct=float(os.getenv("MAX_TRADE_EXPOSURE_PCT", "0.01")),
        min_liquidity_usd=float(os.getenv("MIN_LIQUIDITY_USD", "200.0")),
        max_slippage_pct=float(os.getenv("MAX_SLIPPAGE_PCT", "0.0075")),
        high_prob_threshold=float(os.getenv("HIGH_PROB_THRESHOLD", "0.99")),
        vpn_enabled=_parse_bool(os.getenv("VPN_ENABLED"), default=False),
        vpn_reconnect_seconds=int(os.getenv("VPN_RECONNECT_SECONDS", "60")),
        openvpn_config_file=os.getenv("OPENVPN_CONFIG_FILE", "").strip(),
        openvpn_executable=os.getenv("OPENVPN_EXECUTABLE", "openvpn").strip(),
        openvpn_auth_file=os.getenv("OPENVPN_AUTH_FILE", "").strip(),
    )
