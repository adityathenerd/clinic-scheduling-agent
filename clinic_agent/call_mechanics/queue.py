"""A fail-fast bounded queue for one actor mailbox."""

from __future__ import annotations

import asyncio
from typing import Generic, TypeVar, cast


T = TypeVar("T")
_CLOSED = object()


class QueueOverflowError(RuntimeError):
    """The actor cannot safely accept more work."""


class QueueClosedError(RuntimeError):
    """The actor mailbox has reached a terminal state."""


class BoundedAsyncQueue(Generic[T]):
    def __init__(self, capacity: int) -> None:
        if capacity < 1:
            raise ValueError("capacity must be positive")
        self._queue: asyncio.Queue[T | object] = asyncio.Queue(maxsize=capacity)
        self._closed = False

    @property
    def closed(self) -> bool:
        return self._closed

    @property
    def size(self) -> int:
        return self._queue.qsize()

    def put_nowait(self, item: T) -> None:
        if self._closed:
            raise QueueClosedError("queue is closed")
        try:
            self._queue.put_nowait(item)
        except asyncio.QueueFull as exc:
            raise QueueOverflowError("queue capacity exceeded") from exc

    async def get(self) -> T | None:
        item = await self._queue.get()
        if item is _CLOSED:
            return None
        return cast(T, item)

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            self._queue.put_nowait(_CLOSED)
        except asyncio.QueueFull:
            # The consumer is active; it will observe closure after queued work.
            asyncio.create_task(self._queue.put(_CLOSED))

