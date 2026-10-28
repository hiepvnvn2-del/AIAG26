"""In-process pub/sub used to stream worker events to every WebSocket client."""
from __future__ import annotations
import asyncio
import base64
import time
import uuid
from typing import Any, AsyncIterator

class EventBus:
    def __init__(self) -> None:
        self._subs: set[asyncio.Queue] = set()
        self._history: list[dict] = []  # small replay buffer for late joiners
        self._lock = asyncio.Lock()

    async def publish(self, kind: str, payload: dict[str, Any], *, workflow_id: str | None = None) -> None:
        evt = {
            "id": uuid.uuid4().hex[:12],
            "ts": time.time(),
            "kind": kind,
            "workflow_id": workflow_id,
            "payload": payload,
        }
        async with self._lock:
            self._history.append(evt)
            if len(self._history) > 500:
                self._history = self._history[-500:]
            queues = list(self._subs)
        for q in queues:
            try:
                q.put_nowait(evt)
            except asyncio.QueueFull:
                # Drop the oldest for this subscriber rather than blocking the producer.
                try:
                    q.get_nowait()
                    q.put_nowait(evt)
                except Exception:
                    pass

    async def subscribe(self) -> AsyncIterator[dict]:
        q: asyncio.Queue = asyncio.Queue(maxsize=1000)
        async with self._lock:
            self._subs.add(q)
            backlog = self._history[-50:]
        try:
            for evt in backlog:
                yield evt
            while True:
                yield await q.get()
        finally:
            async with self._lock:
                self._subs.discard(q)


bus = EventBus()


async def emit_screenshot(page, workflow_id: str, step: str, quality: int = 60) -> str:
    """Capture a screenshot and broadcast it base64; returns the b64 string."""
    raw = await page.screenshot(type="jpeg", quality=quality)
    b64 = base64.b64encode(raw).decode("ascii")
    await bus.publish(
        "screenshot",
        {"step": step, "image_b64": b64, "mime": "image/jpeg"},
        workflow_id=workflow_id,
    )
    return b64