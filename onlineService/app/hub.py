"""In-process SSE event fan-out."""

import asyncio
import contextlib
from typing import Any


class EventHub:
    def __init__(self) -> None:
        self._queues: list[asyncio.Queue[dict[str, Any]]] = []

    def subscribe(self) -> asyncio.Queue[dict[str, Any]]:
        q: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=256)
        self._queues.append(q)
        return q

    def unsubscribe(self, q: asyncio.Queue[dict[str, Any]]) -> None:
        if q in self._queues:
            self._queues.remove(q)

    async def publish(self, event: dict[str, Any]) -> None:
        for q in list(self._queues):
            try:
                q.put_nowait(event)
            except asyncio.QueueFull:
                # 队列满时丢弃最旧事件，尽量保留最新状态（避免 finished 等关键事件被吞）。
                try:
                    _ = q.get_nowait()
                except asyncio.QueueEmpty:
                    continue
                # 极端并发下若再次满，放弃本次写入。
                with contextlib.suppress(asyncio.QueueFull):
                    q.put_nowait(event)


hub = EventHub()
