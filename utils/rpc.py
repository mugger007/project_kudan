from __future__ import annotations

import itertools


class RpcRotator:
    def __init__(self, primary: str, fallbacks: list[str]):
        rpc_urls = [primary] + [url for url in fallbacks if url and url != primary]
        self._cycle = itertools.cycle(rpc_urls)
        self._last = primary

    def current(self) -> str:
        return self._last

    def next(self) -> str:
        self._last = next(self._cycle)
        return self._last
