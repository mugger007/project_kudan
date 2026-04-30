from __future__ import annotations

import itertools


class RpcRotator:
    def __init__(self, primary: str, fallbacks: list[str]):
        """Deduplicates URLs and builds an infinite round-robin cycle; primary is always first."""
        rpc_urls = [primary] + [url for url in fallbacks if url and url != primary]
        self._cycle = itertools.cycle(rpc_urls)
        self._last = primary

    def current(self) -> str:
        """Returns the last used RPC URL without advancing the cycle."""
        return self._last

    def next(self) -> str:
        """Advances to the next URL in the round-robin cycle and returns it. Call on connection failure."""
        self._last = next(self._cycle)
        return self._last
