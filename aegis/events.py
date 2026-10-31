"""A small thread-safe publish/subscribe bus that feeds the UI's event stream."""
from __future__ import annotations

import asyncio
import threading
import time
from typing import Any


class Bus:
    def __init__(self) -> None:
        self._subscribers: set[asyncio.Queue] = set()
        self._loop: asyncio.AbstractEventLoop | None = None
        self._lock = threading.Lock()
        self._seq = 0

    def bind(self, loop: asyncio.AbstractEventLoop) -> None:
        self._loop = loop

    def subscribe(self) -> asyncio.Queue:
        queue: asyncio.Queue = asyncio.Queue(maxsize=5000)
        with self._lock:
            self._subscribers.add(queue)
        return queue

    def unsubscribe(self, queue: asyncio.Queue) -> None:
        with self._lock:
            self._subscribers.discard(queue)

    def publish(self, event: dict[str, Any]) -> None:
        """Safe from any thread."""
        with self._lock:
            self._seq += 1
            event = {**event, "seq": self._seq, "ts": time.time()}
            targets = list(self._subscribers)
        loop = self._loop
        if loop is None or loop.is_closed():
            return
        for queue in targets:
            loop.call_soon_threadsafe(self._offer, queue, event)

    @staticmethod
    def _offer(queue: asyncio.Queue, event: dict[str, Any]) -> None:
        try:
            queue.put_nowait(event)
        except asyncio.QueueFull:  # a stalled client reloads its state when it reconnects
            pass
