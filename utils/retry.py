from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from typing import TypeVar

T = TypeVar("T")


async def async_retry(
    func: Callable[[], Awaitable[T]],
    retries: int = 3,
    base_delay: float = 0.5,
    retry_on: tuple[type[BaseException], ...] = (Exception,),
) -> T:
    attempt = 0
    while True:
        try:
            return await func()
        except retry_on:
            attempt += 1
            if attempt > retries:
                raise
            await asyncio.sleep(base_delay * (2 ** (attempt - 1)))
