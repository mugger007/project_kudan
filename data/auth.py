from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import Any

from py_clob_client.client import ClobClient as PolyClobClient
from py_clob_client.clob_types import ApiCreds, RequestArgs
from py_clob_client.headers.headers import create_level_2_headers
from py_clob_client.signer import Signer


class ClobAuthError(RuntimeError):
    """Raised when authenticated CLOB calls cannot be signed."""


@dataclass(slots=True)
class ClobApiCredentials:
    """Holds CLOB L2 credentials used to sign authenticated requests."""

    api_key: str
    api_secret: str
    api_passphrase: str

    def to_sdk_creds(self) -> ApiCreds:
        """Converts credentials into the SDK-native credential dataclass."""
        return ApiCreds(
            api_key=self.api_key,
            api_secret=self.api_secret,
            api_passphrase=self.api_passphrase,
        )

    @classmethod
    def from_any(cls, value: Any) -> "ClobApiCredentials":
        """Builds credentials from an SDK object or a mapping-like response."""
        if isinstance(value, ApiCreds):
            return cls(
                api_key=value.api_key,
                api_secret=value.api_secret,
                api_passphrase=value.api_passphrase,
            )

        mapping = value if isinstance(value, dict) else {}

        api_key = str(getattr(value, "api_key", "") or mapping.get("apiKey") or mapping.get("api_key") or "")
        api_secret = str(
            getattr(value, "api_secret", "")
            or mapping.get("secret")
            or mapping.get("apiSecret")
            or mapping.get("api_secret")
            or ""
        )
        api_passphrase = str(
            getattr(value, "api_passphrase", "")
            or mapping.get("passphrase")
            or mapping.get("apiPassphrase")
            or mapping.get("api_passphrase")
            or ""
        )

        if not api_key or not api_secret or not api_passphrase:
            raise ClobAuthError("Derived CLOB API credentials are incomplete")

        return cls(api_key=api_key, api_secret=api_secret, api_passphrase=api_passphrase)


class ClobAuthManager:
    """Manages CLOB API credential lifecycle and L2 header signing."""

    def __init__(
        self,
        host: str,
        chain_id: int,
        private_key: str,
        logger: logging.Logger,
        api_key: str = "",
        api_secret: str = "",
        api_passphrase: str = "",
    ):
        """Initializes signer state and optional pre-existing API credentials."""
        self.host = host.rstrip("/")
        self.chain_id = chain_id
        self.private_key = private_key.strip()
        self.logger = logger
        self._signer = Signer(private_key=self.private_key, chain_id=chain_id)

        self._creds = None
        if api_key and api_secret and api_passphrase:
            self._creds = ClobApiCredentials(
                api_key=api_key,
                api_secret=api_secret,
                api_passphrase=api_passphrase,
            )

        self._sdk_client = PolyClobClient(
            host=self.host,
            chain_id=self.chain_id,
            key=self.private_key,
            creds=self._creds.to_sdk_creds() if self._creds else None,
        )

    async def ensure_api_credentials(self) -> ClobApiCredentials:
        """Creates or derives API credentials via L1 signing if not already loaded."""
        if self._creds is not None:
            return self._creds

        self.logger.info("CLOB auth: deriving API credentials from wallet signer")

        def _derive() -> Any:
            return self._sdk_client.create_or_derive_api_creds()

        last_error: Exception | None = None
        for attempt in range(1, 4):
            try:
                derived = await asyncio.to_thread(_derive)
                self._creds = ClobApiCredentials.from_any(derived)
                self._sdk_client.set_api_creds(self._creds.to_sdk_creds())
                return self._creds
            except Exception as exc:
                last_error = exc
                backoff = min(2 ** attempt, 8)
                self.logger.warning(
                    "CLOB auth derive attempt %s/3 failed: %s (retry in %ss)",
                    attempt,
                    exc,
                    backoff,
                )
                await asyncio.sleep(backoff)

        raise ClobAuthError(f"Failed to derive CLOB API credentials after retries: {last_error}")

    async def build_level2_headers(
        self,
        method: str,
        request_path: str,
        body: Any = None,
        serialized_body: str | None = None,
    ) -> dict[str, str]:
        """Builds POLY_* L2 authentication headers for an HTTP request."""
        creds = await self.ensure_api_credentials()
        request_args = RequestArgs(
            method=method.upper(),
            request_path=request_path,
            body=body,
            serialized_body=serialized_body,
        )
        headers = create_level_2_headers(
            signer=self._signer,
            creds=creds.to_sdk_creds(),
            request_args=request_args,
        )
        return {str(k): str(v) for k, v in headers.items()}