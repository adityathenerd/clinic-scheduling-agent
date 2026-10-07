"""Bounded async workers shared across independently owned call actors."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from typing import TypeVar


T = TypeVar("T")


class AsyncWorkerPool:
    def __init__(self, max_concurrency: int) -> None:
        if max_concurrency < 1:
            raise ValueError("max_concurrency must be positive")
        self._semaphore = asyncio.Semaphore(max_concurrency)
        self._tasks: set[asyncio.Task[object]] = set()
        self._active = 0
        self._peak_active = 0

    @property
    def active(self) -> int:
        return self._active

    @property
    def peak_active(self) -> int:
        return self._peak_active

    def submit(self, operation: Callable[[], Awaitable[T]]) -> asyncio.Task[T]:
        async def run() -> T:
            async with self._semaphore:
                self._active += 1
                self._peak_active = max(self._peak_active, self._active)
                try:
                    return await operation()
                finally:
                    self._active -= 1

        task = asyncio.create_task(run())
        self._tasks.add(task)  # type: ignore[arg-type]
        task.add_done_callback(self._tasks.discard)  # type: ignore[arg-type]
        return task

    async def wait_idle(self) -> None:
        while self._tasks:
            await asyncio.gather(*tuple(self._tasks), return_exceptions=True)

